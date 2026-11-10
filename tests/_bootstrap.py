"""测试引导：把 astrbot 桩和插件包塞进 sys.path，并导出常用对象。

任何 test_*.py 第一行 `import _bootstrap as B` 即可。
"""

from __future__ import annotations

import importlib
import json
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

# IsolatedAsyncioTestCase 开着 debug 模式，慢回调会把日志刷屏
logging.getLogger("asyncio").setLevel(logging.ERROR)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

for _p in (str(HERE / "stub"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PLUGIN_DIR = ROOT / "astrbot_plugin_log"
PKG = "train_log_pkg"

if PKG not in sys.modules:
    _pkg = type(sys)(PKG)
    _pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG] = _pkg

main = importlib.import_module(f"{PKG}.main")
config = importlib.import_module(f"{PKG}.log_collector.config")
session = importlib.import_module(f"{PKG}.log_collector.session")
record = importlib.import_module(f"{PKG}.log_collector.record")
media = importlib.import_module(f"{PKG}.log_collector.media")
writer = importlib.import_module(f"{PKG}.log_collector.writer")
ingestor = importlib.import_module(f"{PKG}.log_collector.ingestor")
stats = importlib.import_module(f"{PKG}.log_collector.stats")

import astrbot.api.message_components as Comp  # noqa: E402
from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api import logger as stub_logger  # noqa: E402

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63f8cfc0f01f00050001ffabce36890000000049454e44ae426082"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32


def make_event(
    *,
    message_type: str = "group",
    group_id: str = "123456789",
    user_id: str = "111",
    text: str = "",
    msg_id: str = "m1",
    ts: int = 1756700000000,
    self_id: str = "10001",
    sub_type: str = "",
    components: Any = None,
    notice: str = "",
    raw_extra: Dict[str, Any] | None = None,
    raw_as_object: bool = False,
    drop_raw: bool = False,
    timestamp: Any = None,
) -> AstrMessageEvent:
    comps = list(components) if components is not None else ([Comp.Plain(text)] if text else [])
    raw: Dict[str, Any] = {
        "post_type": "notice" if notice else "message",
        "message_type": message_type,
        "sub_type": sub_type,
        "self_id": self_id,
        "user_id": user_id,
        "group_id": group_id,
        "message_id": msg_id,
        "time": ts // 1000,
        "sender": {
            "nickname": f"nick{user_id}",
            "card": f"card{user_id}",
            "role": "member",
        },
    }
    if notice:
        raw["notice_type"] = notice
        raw["operator_id"] = user_id
    if raw_extra:
        raw.update(raw_extra)

    raw_obj: Any = raw
    if raw_as_object:
        raw_obj = SimpleNamespace(**raw)

    obj = SimpleNamespace(
        type=SimpleNamespace(
            name="GROUP_MESSAGE" if message_type == "group" else "FRIEND_MESSAGE"
        ),
        self_id=self_id,
        message_id=msg_id,
        group_id=group_id,
        sender=SimpleNamespace(user_id=user_id, nickname=f"nick{user_id}"),
        message=comps,
        message_str=text,
        raw_message=None if drop_raw else raw_obj,
        timestamp=ts if timestamp is None else timestamp,
    )
    return AstrMessageEvent(obj)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def all_files(root: Path, pattern: str = "*.jsonl") -> List[Path]:
    return sorted(p for p in Path(root).rglob(pattern) if p.name != "_manifest.jsonl")


def all_records(root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for f in all_files(root):
        rows.extend(read_jsonl(f))
    return rows


def lines_of(path: Path) -> int:
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/png":
            self._send(200, PNG, "image/png")
        elif path == "/jpeg":
            self._send(200, JPEG, "image/jpeg")
        elif path == "/gif":
            self._send(200, GIF, "image/gif")
        elif path == "/octet":
            self._send(200, PNG, "application/octet-stream")
        elif path == "/text":
            self._send(200, b"hello world", "text/plain; charset=utf-8")
        elif path == "/empty":
            self._send(200, b"", "image/png")
        elif path == "/big":
            self._send(200, b"x" * (3 * 1024 * 1024), "application/octet-stream")
        elif path == "/slow":
            time.sleep(1.2)
            self._send(200, PNG, "image/png")
        else:
            self.send_response(404)
            self.end_headers()

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _QuietHTTP(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # 客户端超时断开是预期行为，不要污染测试输出


class LocalHTTP:
    """本地静态服务，用来测下载/超时/404/超大。"""

    def __init__(self) -> None:
        self.srv = _QuietHTTP(("127.0.0.1", 0), _Handler)
        self.port = self.srv.server_address[1]
        self._t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._t.start()

    def url(self, path: str = "/png") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        try:
            self.srv.shutdown()
        except Exception:
            pass
        try:
            self.srv.server_close()
        except Exception:
            pass

    def __enter__(self) -> "LocalHTTP":
        return self

    def __exit__(self, *exc) -> None:
        self.close()