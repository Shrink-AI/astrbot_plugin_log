"""采集管线：事件 -> 记录 -> 媒体落盘 -> 写入队列。

事件回调只做一次入队（非阻塞），真正的下载与写盘都在后台，
因此即使磁盘卡住也不会拖慢 bot 的消息处理。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

from .config import CollectConfig
from .media import MediaStore
from .record import Envelope, build_envelope
from .stats import Stats
from .writer import ShardedWriter


class Ingestor:
    def __init__(self, cfg: CollectConfig, logger=None) -> None:
        self.cfg = cfg
        self.logger = logger
        self.stats = Stats()
        self.root = cfg.resolve_root()

        self.writer = ShardedWriter(
            root=self.root,
            max_file_bytes=cfg.max_file_bytes,
            flush_interval=0.2,
            fsync_interval=float(cfg.fsync_interval_s),
            logger=logger,
            stats=self.stats,
        )
        self.media: Optional[MediaStore] = None
        if cfg.download_media:
            self.media = MediaStore(
                root=self.root,
                max_bytes=cfg.media_max_bytes,
                timeout=float(cfg.media_timeout_s),
                concurrency=cfg.media_concurrency,
                logger=logger,
                stats=self.stats,
            )

        self._q: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._dedup: "OrderedDict[Tuple[str, str], int]" = OrderedDict()
        self._seq = 0
        self._last_stats = time.time()
        self._lock_retry_at = 0.0
        self._closing = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._q = asyncio.Queue(maxsize=max(64, int(self.cfg.queue_size)))
        self._task = asyncio.create_task(self._consume(), name="train-log-ingest")

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._q is not None:
            try:
                self._q.put_nowait(None)
            except asyncio.QueueFull:
                if self._task is not None:
                    self._task.cancel()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        self.writer.close()
        self._log_stats(force=True)

    # ------------------------------------------------------------------ ingest
    def offer(self, event: Any) -> bool:
        """事件回调里调用；必须极快且不抛异常。"""
        if self._q is None or self._closing:
            return False
        try:
            self._seq += 1
            env = build_envelope(event, self.cfg, self._seq)
        except Exception as e:
            self.stats.inc("parse_error")
            self._warn("build record failed", e)
            return False
        if env is None:
            self.stats.inc("ignored")
            return False

        rec = env.record
        if not self._wanted(rec):
            self.stats.inc("filtered")
            return False

        if rec.get("type") == "message":
            msg_id = rec.get("msg_id") or ""
            conv_key = (rec.get("conv") or {}).get("key") or ""
            # 带上会话，避免不同会话的短 msg_id 相互误伤
            if msg_id and self._duplicate(rec.get("self_id") or "", conv_key, msg_id):
                self.stats.inc("duplicated")
                return False

        try:
            self._q.put_nowait(env)
        except asyncio.QueueFull:
            self.stats.inc("dropped_queue")
            self._warn("queue full, message dropped", None)
            return False
        self.stats.inc("received")
        return True

    def _wanted(self, rec: Dict[str, Any]) -> bool:
        conv = rec.get("conv") or {}
        kind = conv.get("kind")
        if kind == "group":
            if not self.cfg.collect_group:
                return False
            if not self.cfg.group_allowed(conv.get("id") or ""):
                return False
        elif kind == "private":
            if not self.cfg.collect_private:
                return False
        elif kind == "temp":
            if not self.cfg.collect_temp_session:
                return False
        if rec.get("type") == "message" and self.cfg.ignore_self:
            if (rec.get("sender") or {}).get("is_self"):
                return False
        return True

    def _duplicate(self, self_id: str, conv_key: str, msg_id: str) -> bool:
        key = (self_id, conv_key, msg_id)
        if key in self._dedup:
            self._dedup.move_to_end(key)
            return True
        self._dedup[key] = 1
        limit = max(1000, int(self.cfg.dedup_cache_size))
        while len(self._dedup) > limit:
            self._dedup.popitem(last=False)
        return False

    # ------------------------------------------------------------------ consumer
    async def _consume(self) -> None:
        assert self._q is not None
        while True:
            env = await self._q.get()
            try:
                if env is None:
                    return
                self._maybe_take_over()
                await self._resolve_media(env.record)
                line = json.dumps(env.record, ensure_ascii=False, separators=(",", ":"))
                if self.writer.submit(env.conv_rel, env.day, line):
                    self.stats.inc("written")
                else:
                    self.stats.inc("dropped_writer")
            except asyncio.CancelledError:
                self._q.task_done()
                raise
            except Exception as e:
                self.stats.inc("write_error")
                self._warn("consume failed", e)
            finally:
                try:
                    self._q.task_done()
                except ValueError:
                    pass
                self._maybe_log_stats()

    def _maybe_take_over(self) -> None:
        """热重载时旧实例可能还没退干净，导致本实例启动时抢不到目录锁。

        被锁在外面时每 30 秒重试一次，旧实例一退出就接管，不用重启 bot。
        """
        if self.writer.locked:
            return
        now = time.monotonic()
        if now < self._lock_retry_at:
            return
        self._lock_retry_at = now + 30.0
        self.writer.retry_lock()

    async def _resolve_media(self, rec: Dict[str, Any]) -> None:
        if self.media is None:
            return
        wanted = {str(k).strip().lower() for k in (self.cfg.media_kinds or [])}
        if not wanted:
            return
        segments = (rec.get("msg") or {}).get("segments") or []

        jobs = []
        for seg in segments:
            if seg.get("t") not in wanted:
                continue
            url = seg.get("url") or ""
            if not str(url).startswith(("http://", "https://")):
                file_ref = seg.get("file") or ""
                url = file_ref if str(file_ref).startswith(("http://", "https://")) else ""
            if not url:
                seg.setdefault("err", "no_url")
                continue
            jobs.append((seg, url))

        if not jobs:
            return

        results = await asyncio.gather(
            *(self.media.fetch(u) for _, u in jobs), return_exceptions=True
        )
        for (seg, _url), res in zip(jobs, results):
            if isinstance(res, BaseException):
                seg["err"] = f"{type(res).__name__}: {res}"[:200]
                self.stats.inc("media_error")
                continue
            if not res:
                seg["err"] = "empty"
                self.stats.inc("media_error")
                continue
            if "err" in res:
                seg["err"] = res["err"]
                self.stats.inc("media_error")
                continue
            seg.update(res)
            self.stats.inc("media_ok")

    # ------------------------------------------------------------------ misc
    def _maybe_log_stats(self) -> None:
        interval = float(self.cfg.log_stats_interval_s)
        if interval <= 0:
            return
        now = time.time()
        if now - self._last_stats >= interval:
            self._log_stats()

    def _log_stats(self, force: bool = False) -> None:
        self._last_stats = time.time()
        if self.logger is None:
            return
        try:
            snap = self.stats.snapshot()
            snap["writer_bytes"] = self.writer.bytes_written
            snap["writer_dropped"] = self.writer.dropped
            self.logger.info(f"[train_log] stats: {snap}")
        except Exception:
            pass

    def _warn(self, msg: str, err: Optional[BaseException]) -> None:
        if self.logger is None:
            return
        try:
            if err is None:
                self.logger.warning(f"[train_log] {msg}")
            else:
                self.logger.warning(f"[train_log] {msg}: {type(err).__name__}: {err}")
        except Exception:
            pass