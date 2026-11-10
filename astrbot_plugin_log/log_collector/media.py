"""媒体落盘：内容寻址（sha256）保存图片/语音/视频。

QQ 的媒体 URL 会过期，所以必须在收到消息时立刻抓取。这里只负责下载与去重，
不做任何模型相关处理。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AstrBot-train-log/1.0"

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "audio/amr": ".amr",
    "audio/silk": ".silk",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}


def _pick_backend() -> str:
    try:
        import httpx  # noqa: F401

        return "httpx"
    except ImportError:
        pass
    try:
        import aiohttp  # noqa: F401

        return "aiohttp"
    except ImportError:
        return "urllib"


def _drop(tmp: Path) -> None:
    """删掉临时文件；已经被 os.replace 移走时静默跳过。"""
    try:
        tmp.unlink()
    except OSError:
        pass


def _sniff(data: bytes) -> Tuple[str, str]:
    """(mime, ext) —— 先看魔数，图片格式几乎都能认出来。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    if data[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    return "application/octet-stream", ".bin"


class MediaStore:
    def __init__(
        self,
        root: Path,
        max_bytes: int,
        timeout: float,
        concurrency: int,
        logger=None,
        stats=None,
    ) -> None:
        self.root = Path(root)
        self.dir = self.root / "media"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(1024, int(max_bytes))
        self.timeout = max(1.0, float(timeout))
        self.logger = logger
        self.stats = stats
        self.backend = _pick_backend()
        self._sem = asyncio.Semaphore(max(1, int(concurrency)))

    # ------------------------------------------------------------------ public
    async def fetch(self, url: str) -> Dict[str, Any]:
        """下载并落盘；失败时返回 {"err": ...}，绝不抛异常。"""
        async with self._sem:
            try:
                data, mime = await self._download(url)
            except Exception as e:  # 网络/超时/4xx 全都在这里
                return {"err": f"{type(e).__name__}: {e}"[:200], "url": url}
        if not data:
            return {"err": "empty", "url": url}
        if len(data) > self.max_bytes:
            return {"err": "too_large", "url": url, "bytes": len(data)}
        try:
            return self.store_bytes(data, mime)
        except Exception as e:  # 磁盘满 / 权限问题也不能把异常抛回采集链路
            return {"err": f"{type(e).__name__}: {e}"[:200], "url": url}

    def store_bytes(self, data: bytes, mime: str = "") -> Dict[str, Any]:
        sha = hashlib.sha256(data).hexdigest()
        magic_mime, magic_ext = _sniff(data)
        header_mime = (mime or "").split(";")[0].strip().lower()

        # 以文件内容为准：服务端 Content-Type 经常是 application/octet-stream 或错的
        if magic_mime != "application/octet-stream":
            final_mime, ext = magic_mime, magic_ext
        else:
            final_mime = header_mime or magic_mime
            ext = _MIME_EXT.get(final_mime, ".bin")

        rel = f"media/{sha[:2]}/{sha}{ext}"
        path = self.root / rel
        ok = {"sha256": sha, "path": rel, "bytes": len(data), "mime": final_mime}
        if path.exists():
            return ok

        # 同一份内容可能被多个任务同时写入；临时名唯一，落盘靠原子改名
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            # Windows 上并发改名同一目标会被短暂拒绝；内容寻址下
            # 「目标已存在且大小一致」等价于写入成功
            try:
                landed = path.stat().st_size == len(data)
            except OSError:
                landed = False
            if not landed:
                return {"err": f"{type(e).__name__}: {e}"[:200], "sha256": sha}
        finally:
            _drop(tmp)
        return ok

    # ----------------------------------------------------------------- private
    async def _download(self, url: str) -> Tuple[bytes, str]:
        if self.backend == "httpx":
            import httpx

            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, headers={"User-Agent": _UA}
            ) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            break  # 超限就停，交给 fetch 判 too_large
                    return bytes(buf), resp.headers.get("content-type", "")

        if self.backend == "aiohttp":
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(headers={"User-Agent": _UA}) as session:
                async with session.get(url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            break
                    return bytes(buf), resp.headers.get("content-type", "")

        return await asyncio.to_thread(self._urllib_get, url)

    def _urllib_get(self, url: str) -> Tuple[bytes, str]:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            data = resp.read(self.max_bytes + 1)
            return data, resp.headers.get("Content-Type", "")