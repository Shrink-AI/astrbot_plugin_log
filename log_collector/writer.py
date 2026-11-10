"""按会话 + 日期 + 大小滚动的 JSONL 写入器。

- bot 侧只做入队，磁盘 IO 全部在独立线程
- 每个会话一个文件句柄，LRU 上限，避免每条消息 open/close（Windows 上尤其要命）
- 崩溃安全：追加写 + 打开时截断尾部半行
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_CHUNK = 8192

# 抢锁失败的错误码：Windows 上是 EACCES；POSIX 上是 EAGAIN/EACCES/EDEADLK
_LOCK_CONFLICT_ERRNOS = {11, 13, 35, 36, 72}
_LOCK_CONFLICT_WINERRORS = {33, 36}


def _is_lock_conflict(err: BaseException) -> bool:
    if getattr(err, "winerror", None) in _LOCK_CONFLICT_WINERRORS:
        return True
    return getattr(err, "errno", None) in _LOCK_CONFLICT_ERRNOS


class ShardedWriter:
    def __init__(
        self,
        root: Path,
        max_file_bytes: int,
        flush_interval: float = 0.2,
        fsync_interval: float = 5.0,
        max_open_files: int = 128,
        queue_size: int = 50000,
        logger=None,
        stats=None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = max(64 * 1024, int(max_file_bytes))
        self.flush_interval = max(0.02, float(flush_interval))
        self.fsync_interval = max(0.0, float(fsync_interval))
        self.max_open_files = max(8, int(max_open_files))
        self.logger = logger
        self.stats = stats

        self._q: "queue.Queue[Tuple[str, str, bytes]]" = queue.Queue(maxsize=max(1024, int(queue_size)))
        self._state: Dict[str, Tuple[str, int, int]] = {}
        self._pending: Dict[Path, List[bytes]] = {}
        self._handles: "OrderedDict[Path, Any]" = OrderedDict()
        self._manifest_fh = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._last_fsync = time.time()
        self.dropped = 0
        self.written = 0
        self.bytes_written = 0

        # 同一个数据目录只允许一个写入者。两个写入者同时 append 同一文件时，
        # 大块写入会被操作系统拆开、互相穿插，实测会静默丢记录（1%~3% 概率）。
        # 拿不到锁就不启动写入线程，宁可明确报错也不要写坏数据。
        self._lock_fh = None
        self.locked = self._try_lock()
        self._thread = None
        if self.locked:
            self._thread = threading.Thread(target=self._run, name="train-log-writer", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ lock
    def _try_lock(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fh = open(self.root / ".writer.lock", "a+b")
        except OSError as e:
            self._warn("创建锁文件失败，跳过独占检查", e)
            return True
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - POSIX 分支，本机（Windows）跑不到
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            try:
                fh.close()
            except OSError:
                pass
            if _is_lock_conflict(e):
                self._warn(f"数据目录已被另一个实例占用（{self.root}），本实例不写盘", e)
                return False
            # 有些网络盘/只读盘不支持字节锁（ENOLCK/EOPNOTSUPP），不能因此停采
            self._warn("文件系统不支持独占锁，跳过实例冲突检查", e)
            return True
        self._lock_fh = fh
        return True

    def _release_lock(self) -> None:
        fh, self._lock_fh = self._lock_fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - POSIX 分支，本机（Windows）跑不到
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        try:
            fh.close()
        except OSError:
            pass

    def retry_lock(self) -> bool:
        """尝试接管数据目录（旧实例已退出时用），成功则启动写入线程。"""
        if self.locked or self._closed:
            return self.locked
        if not self._try_lock():
            return False
        self.locked = True
        self._thread = threading.Thread(target=self._run, name="train-log-writer", daemon=True)
        self._thread.start()
        self._warn("已接管数据目录，恢复写盘", None)
        return True

    # ------------------------------------------------------------------ public
    def submit(self, conv_rel: str, day: str, line: str) -> bool:
        if self._closed or not self.locked:
            with self._lock:
                self.dropped += 1
            return False
        # 半截 emoji 会产生孤立代理项（\ud800），直接 UTF-8 编码会抛异常把整条记录丢掉；
        # backslashreplace 把它变成 \ud800 转义，仍是合法 JSON，内容可原样读回。
        data = (line.rstrip("\n") + "\n").encode("utf-8", "backslashreplace")
        try:
            self._q.put_nowait((conv_rel, day, data))
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def close(self, timeout: float = 15.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                # 线程没来得及收尾，主线程直接兜底落盘
                self._drain()
                self._flush()
        self._close_handles()
        if self._manifest_fh is not None:
            try:
                self._manifest_fh.close()
            except OSError:
                pass
            self._manifest_fh = None
        self._release_lock()

    # ------------------------------------------------------------------ thread
    def _run(self) -> None:
        while True:
            stopping = self._stop.is_set()
            if stopping and self._q.empty():
                self._drain()
                self._flush(force_fsync=True)
                self._close_handles()
                return

            batch: List[Tuple[str, str, bytes]] = []
            try:
                batch.append(self._q.get(timeout=self.flush_interval))
            except queue.Empty:
                pass
            else:
                while len(batch) < _CHUNK:
                    try:
                        batch.append(self._q.get_nowait())
                    except queue.Empty:
                        break

            for conv_rel, day, data in batch:
                try:
                    self._stage(conv_rel, day, data)
                except Exception as e:  # 单条失败不能拖垮整个写入线程
                    self._warn("stage failed", e)
                    with self._lock:
                        self.dropped += 1

            self._flush()

    def _drain(self) -> None:
        while True:
            try:
                conv_rel, day, data = self._q.get_nowait()
            except queue.Empty:
                return
            try:
                self._stage(conv_rel, day, data)
            except Exception as e:
                self._warn("stage failed", e)

    # ------------------------------------------------------------------ staging
    def _stage(self, conv_rel: str, day: str, data: bytes) -> None:
        path = self._resolve(conv_rel, day, len(data))
        self._pending.setdefault(path, []).append(data)

    def _resolve(self, conv_rel: str, day: str, nbytes: int) -> Path:
        state = self._state.get(conv_rel)
        if state is None:
            state = self._resume(conv_rel, day)

        cur_day, part, size = state
        if cur_day != day:
            part = 0
            path = self._path(conv_rel, day, part)
            size = path.stat().st_size if path.exists() else 0
            while path.exists() and size >= self.max_file_bytes:
                part += 1
                path = self._path(conv_rel, day, part)
                size = path.stat().st_size if path.exists() else 0
        elif size > 0 and size + nbytes > self.max_file_bytes:
            part += 1
            path = self._path(conv_rel, day, part)
            size = path.stat().st_size if path.exists() else 0
        else:
            path = self._path(conv_rel, day, part)

        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            self._manifest(conv_rel, day, part, path)

        self._state[conv_rel] = (day, part, size + nbytes)
        return path

    def _resume(self, conv_rel: str, day: str) -> Tuple[str, int, int]:
        """重启后接着写：找该天已有的最大 part，按它的实际大小继续。"""
        directory = self.root / conv_rel
        best = -1
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for f in directory.iterdir():
                name = f.name
                if not name.startswith(day + ".") or not name.endswith(".jsonl"):
                    continue
                try:
                    idx = int(name[len(day) + 1 : -len(".jsonl")])
                except ValueError:
                    continue
                if idx > best:
                    best = idx
        except OSError:
            pass
        if best < 0:
            return (day, 0, 0)
        path = self._path(conv_rel, day, best)
        try:
            return (day, best, path.stat().st_size)
        except OSError:
            return (day, best, 0)

    @staticmethod
    def _fname(day: str, part: int) -> str:
        return f"{day}.{part:03d}.jsonl"

    def _path(self, conv_rel: str, day: str, part: int) -> Path:
        return self.root / conv_rel / self._fname(day, part)

    # ------------------------------------------------------------------ flushing
    def _flush(self, force_fsync: bool = False) -> None:
        if self._pending:
            pending, self._pending = self._pending, {}
            for path, chunks in pending.items():
                data = b"".join(chunks)
                fh = self._handle(path)
                if fh is None:
                    with self._lock:
                        self.dropped += len(chunks)
                    continue
                try:
                    fh.write(data)
                    fh.flush()
                    with self._lock:
                        self.written += len(chunks)
                        self.bytes_written += len(data)
                except Exception as e:
                    self._warn(f"write failed {path}", e)
                    with self._lock:
                        self.dropped += len(chunks)

        now = time.time()
        if force_fsync or (self.fsync_interval > 0 and now - self._last_fsync >= self.fsync_interval):
            self._fsync()
            self._last_fsync = now

    def _fsync(self) -> None:
        for fh in list(self._handles.values()):
            try:
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass

    def _handle(self, path: Path):
        fh = self._handles.get(path)
        if fh is not None:
            self._handles.move_to_end(path)
            return fh
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._repair_tail(path)
            fh = open(path, "ab")
        except OSError as e:
            self._warn(f"open failed {path}", e)
            return None
        self._handles[path] = fh
        if len(self._handles) > self.max_open_files:
            _, old = self._handles.popitem(last=False)
            try:
                old.close()
            except OSError:
                pass
        return fh

    def _repair_tail(self, path: Path) -> None:
        """上次崩溃可能留下半行 JSON，截断到最后一个换行，避免污染后续读取。"""
        try:
            size = path.stat().st_size
            if size == 0:
                return
            with open(path, "rb+") as f:
                f.seek(size - 1)
                if f.read(1) == b"\n":
                    return
                pos = size
                last_nl = -1
                while pos > 0 and last_nl < 0:
                    step = min(8192, pos)
                    pos -= step
                    f.seek(pos)
                    buf = f.read(step)
                    idx = buf.rfind(b"\n")
                    if idx >= 0:
                        last_nl = pos + idx
                f.truncate(last_nl + 1 if last_nl >= 0 else 0)
        except OSError as e:
            self._warn(f"tail repair failed {path}", e)

    def _close_handles(self) -> None:
        for fh in list(self._handles.values()):
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass
            try:
                fh.close()
            except (OSError, ValueError):
                pass
        self._handles.clear()

    def _manifest(self, conv_rel: str, day: str, part: int, path: Path) -> None:
        try:
            if self._manifest_fh is None:
                self._manifest_fh = open(self.root / "_manifest.jsonl", "a", encoding="utf-8")
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            self._manifest_fh.write(
                json.dumps(
                    {
                        "conv": conv_rel,
                        "day": day,
                        "part": part,
                        "path": rel,
                        "opened_at": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            self._manifest_fh.flush()
        except OSError as e:
            self._warn("manifest failed", e)

    def _warn(self, msg: str, err: Optional[BaseException] = None) -> None:
        if self.logger is not None:
            try:
                self.logger.warning(f"[train_log] {msg}: {err}")
            except Exception:
                pass