"""离线自测：在没有 AstrBot 的环境里验证采集内核。

覆盖：
1. 群聊 / 私聊分目录，A-B 与 B-A 同键
2. 跨天分文件
3. 128MB（测试里降到 1MB）滚动
4. 图片下载 + sha256 内容寻址去重 + 404 失败仍落记录
5. (self_id, msg_id) 去重
6. 撤回类 notice 事件落同一会话文件
7. 崩溃残留半行的尾部修复、重启后续写
8. 每行都是合法 JSON 且必备字段齐全

用法: python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "stub"))
sys.path.insert(0, str(ROOT))

import astrbot.api.message_components as Comp  # noqa: E402
from astrbot.api.event import AstrMessageEvent  # noqa: E402

PLUGIN_DIR = ROOT
_pkg = type(sys)("train_log_pkg")
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules["train_log_pkg"] = _pkg
plugin_mod = importlib.import_module("train_log_pkg.main")
session_mod = importlib.import_module("train_log_pkg.log_collector.session")
writer_mod = importlib.import_module("train_log_pkg.log_collector.writer")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILS.append(label)


# ---------------------------------------------------------------- local http
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/img.png"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------- event builder
def make_event(
    *,
    message_type: str,
    group_id: str = "",
    user_id: str,
    text: str = "",
    msg_id: str,
    ts: int,
    self_id: str = "10001",
    sub_type: str = "",
    images: tuple[str, ...] = (),
    notice: str = "",
) -> AstrMessageEvent:
    comps = []
    if text:
        comps.append(Comp.Plain(text=text))
    for url in images:
        comps.append(Comp.Image(file="", url=url))
    if text and "回复" in text:
        comps.insert(0, Comp.Reply(id=f"{msg_id}"))
    raw = {
        "post_type": "notice" if notice else "message",
        "message_type": message_type,
        "sub_type": sub_type,
        "self_id": self_id,
        "user_id": user_id,
        "group_id": group_id,
        "message_id": msg_id,
        "time": ts // 1000,
        "sender": {"nickname": f"nick{user_id}", "card": f"card{user_id}", "role": "member"},
    }
    if notice:
        raw["notice_type"] = notice
    obj = SimpleNamespace(
        type=SimpleNamespace(name="GROUP_MESSAGE" if message_type == "group" else "FRIEND_MESSAGE"),
        self_id=self_id,
        message_id=msg_id,
        group_id=group_id,
        sender=SimpleNamespace(user_id=user_id, nickname=f"nick{user_id}"),
        message=comps,
        message_str=text,
        raw_message=raw,
        timestamp=ts,
    )
    return AstrMessageEvent(obj)


def iter_jsonl(root: Path):
    for p in sorted(root.rglob("*.jsonl")):
        if p.name == "_manifest.jsonl":
            continue
        yield p


def read_lines(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- e2e
async def test_end_to_end(tmp: Path, base_ts: int, img_url: str, bad_url: str):
    cfg = {
        "data_dir": str(tmp),
        "max_file_mb": 1,
        "timezone": "Asia/Shanghai",
        "download_media": True,
        "media_kinds": ["image"],
        "queue_size": 20000,
        "fsync_interval_s": 0,
        "log_stats_interval_s": 0,
    }
    plugin = plugin_mod.TrainLogPlugin(context=None, config=cfg)
    ing = await plugin._get_ingestor()

    day1 = session_mod.day_of(base_ts, cfg["timezone"])
    day2 = session_mod.day_of(base_ts + 40 * 3600 * 1000, cfg["timezone"])
    check(day1 != day2, f"两天落在不同分片 ({day1} / {day2})")
    # 第二天那批消息的 ts 锚到 day2 的 00:00。直接写 base+40h+i*60s 的话，
    # 当 base 的时刻偏晚时会漂到 day3，day2 分片根本不存在——这个用例以前
    # 是看当前时间点碰运气的。
    day2_start = datetime.fromtimestamp((base_ts + 40 * 3600 * 1000) / 1000,
                                        session_mod.get_tz(cfg["timezone"]))
    day2_start_ms = int(day2_start.replace(hour=0, minute=0, second=0,
                                           microsecond=0).timestamp() * 1000)
    check(session_mod.day_of(day2_start_ms, cfg["timezone"]) == day2, "day2 起点算对")

    total = 0
    dup_ids: list[str] = []

    # 群 1001：1200 条，前 600 条 day1，其余 day2，每 25 条一张图
    for i in range(1200):
        ts = base_ts + (i * 60_000 if i < 600 else (day2_start_ms - base_ts) + i * 60_000)
        images = ()
        if i % 25 == 0:
            images = (bad_url,) if i == 500 else (img_url,)
        ev = make_event(
            message_type="group",
            group_id="1001",
            user_id=str(20000 + (i % 7)),
            text=f"群消息 {i} 回复" if i % 50 == 0 else f"群消息 {i}",
            msg_id=f"g1001-{i}",
            ts=ts,
            images=images,
        )
        if i == 0:
            await plugin.on_all_message(ev)  # 走一次真实回调路径
        else:
            ing.offer(ev)
        total += 1
        if i < 5:
            dup_ids.append(f"g1001-{i}")

    # 群 2002：700 条
    for i in range(700):
        ts = base_ts + i * 30_000
        ing.offer(
            make_event(
                message_type="group",
                group_id="2002",
                user_id=str(30000 + (i % 5)),
                text=f"二群 {i}",
                msg_id=f"g2002-{i}",
                ts=ts,
            )
        )
        total += 1

    # 私聊：777 <-> bot(10001)：300 条
    for i in range(300):
        ts = base_ts + i * 90_000
        ing.offer(
            make_event(
                message_type="private",
                user_id="777",
                text=f"私聊 {i}",
                msg_id=f"p777-{i}",
                ts=ts,
            )
        )
        total += 1

    # 重复投递：应被 (self_id, msg_id) 拦掉
    dup_hit = 0
    for mid in dup_ids:
        if not ing.offer(
            make_event(
                message_type="group",
                group_id="1001",
                user_id="20000",
                text="duplicate",
                msg_id=mid,
                ts=base_ts,
            )
        ):
            dup_hit += 1
    check(dup_hit == len(dup_ids), f"重复消息被去重 ({dup_hit}/{len(dup_ids)})")

    # 撤回事件
    ing.offer(
        make_event(
            message_type="group",
            group_id="1001",
            user_id="20001",
            text="",
            msg_id="recall-1",
            ts=base_ts + 1000,
            notice="group_recall",
        )
    )

    await ing.aclose()

    # ---- 磁盘校验
    files = list(iter_jsonl(tmp))
    rows = []
    for f in files:
        rows.extend(read_lines(f))

    msg_rows = [r for r in rows if r.get("type") == "message"]
    notice_rows = [r for r in rows if r.get("type") == "notice"]

    check(len(msg_rows) == total, f"消息条数 {len(msg_rows)} == {total}")
    check(len(notice_rows) == 1, "撤回事件落盘 1 条")

    g1 = [r for r in msg_rows if r["conv"]["key"] == "g/1001"]
    g2 = [r for r in msg_rows if r["conv"]["key"] == "g/2002"]
    pv = [r for r in msg_rows if r["conv"]["kind"] == "private"]
    check(len(g1) == 1200, f"群 1001 条数 {len(g1)}")
    check(len(g2) == 700, f"群 2002 条数 {len(g2)}")
    check(len(pv) == 300, f"私聊条数 {len(pv)}")

    check((tmp / "g" / "1001" / f"{day1}.000.jsonl").exists(), "群 1001 day1 文件存在")
    check((tmp / "g" / "1001" / f"{day2}.000.jsonl").exists(), "群 1001 day2 文件存在")
    check(pv and pv[0]["conv"]["key"] == "p/777_10001", f"私聊目录 {pv[0]['conv']['key'] if pv else '-'}")
    check(notice_rows[0]["conv"]["key"] == "g/1001", "撤回事件落进群会话")

    # 媒体
    media_files = [p for p in (tmp / "media").rglob("*") if p.is_file()]
    check(len(media_files) == 1, f"同一张图只存一份（{len(media_files)} 个文件）")

    ok_segs = [
        s
        for r in msg_rows
        for s in r["msg"]["segments"]
        if s.get("t") == "image" and s.get("sha256")
    ]
    err_segs = [
        s
        for r in msg_rows
        for s in r["msg"]["segments"]
        if s.get("t") == "image" and s.get("err")
    ]
    check(len(ok_segs) == 47, f"成功落盘的图片 {len(ok_segs)} == 47")
    check(len(err_segs) == 1, f"下载失败的图片被标记 {len(err_segs)} == 1")
    if ok_segs:
        s = ok_segs[0]
        p = tmp / s["path"]
        check(p.exists(), "图片文件实际存在")
        import hashlib

        check(hashlib.sha256(p.read_bytes()).hexdigest() == s["sha256"], "sha256 与实际内容一致")

    # 必备字段
    required = ("v", "type", "day", "ts", "recv_ts", "self_id", "msg_id", "conv", "sender", "msg")
    bad = [i for i, r in enumerate(msg_rows) if any(k not in r for k in required)]
    check(not bad, f"必备字段齐全（缺失 {len(bad)} 条）")

    reply_rows = [r for r in msg_rows if r["msg"]["reply_to"]]
    check(len(reply_rows) == 24, f"reply 关系被保留 {len(reply_rows)} == 24")

    texts = [r["msg"]["text"] for r in g1[:10]]
    check(all(texts), "text 派生字段非空")

    # 统计
    print("\n  --- 分布 ---")
    print(f"  文件数: {len(files)}, 记录数: {len(rows)}")
    for f in files[:8]:
        print(f"    {f.relative_to(tmp).as_posix()}  {f.stat().st_size} B")


# ---------------------------------------------------------------- writer unit
def test_writer_rolling(tmp: Path):
    w = writer_mod.ShardedWriter(
        root=tmp, max_file_bytes=64 * 1024, flush_interval=0.05, fsync_interval=0, logger=None
    )
    n = 3000
    line = json.dumps({"v": 1, "pad": "x" * 160}, ensure_ascii=False)
    for i in range(n):
        w.submit("g/9", "2025-01-01", line)
    w.close()

    parts = sorted((tmp / "g" / "9").glob("2025-01-01.*.jsonl"))
    check(len(parts) > 1, f"超过 64KB 后滚动（{len(parts)} 个 part）")
    total = 0
    oversize = 0
    for p in parts:
        total += len(read_lines(p))
        if p.stat().st_size > 64 * 1024 + 4096:
            oversize += 1
    check(total == n, f"滚动后总行数不丢不重 {total} == {n}")
    check(oversize == 0, f"没有 part 明显超限（{oversize} 个）")


def test_writer_tail_repair(tmp: Path):
    d = tmp / "g" / "77"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "2025-02-02.000.jsonl"
    f.write_bytes(b'{"v":1,"good":true}\n{"v":1,"half":tr')  # 模拟崩溃留下的半行

    w = writer_mod.ShardedWriter(
        root=tmp, max_file_bytes=1024 * 1024, flush_interval=0.05, fsync_interval=0, logger=None
    )
    w.submit("g/77", "2025-02-02", json.dumps({"v": 1, "new": True}))
    w.close()

    rows = read_lines(f)
    check(len(rows) == 2, f"半行被截断，剩完整记录 {len(rows)} 条")
    check(rows[-1].get("new") is True, "新记录正确接在后面")


def test_writer_resume(tmp: Path):
    d = tmp / "g" / "88"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "2025-03-03.000.jsonl"
    f.write_text(json.dumps({"v": 1, "old": 1}) + "\n", encoding="utf-8")

    w = writer_mod.ShardedWriter(
        root=tmp, max_file_bytes=1024 * 1024, flush_interval=0.05, fsync_interval=0, logger=None
    )
    w.submit("g/88", "2025-03-03", json.dumps({"v": 1, "new": 2}))
    w.close()

    parts = sorted(d.glob("2025-03-03.*.jsonl"))
    check(len(parts) == 1, f"重启后接着写同一个 part（{len(parts)} 个文件）")
    check(len(read_lines(f)) == 2, "旧数据保留")


def main():
    import time

    base_ts = int(time.time() * 1000) - 7 * 24 * 3600 * 1000
    srv, port = start_server()
    img_url = f"http://127.0.0.1:{port}/img.png"
    bad_url = f"http://127.0.0.1:{port}/missing.png"

    with tempfile.TemporaryDirectory(prefix="trainlog-e2e-") as td:
        print("\n[1] 端到端采集")
        asyncio.run(test_end_to_end(Path(td), base_ts, img_url, bad_url))

    with tempfile.TemporaryDirectory(prefix="trainlog-w-") as td:
        print("\n[2] 文件滚动")
        test_writer_rolling(Path(td))
        print("\n[3] 尾部半行修复")
        test_writer_tail_repair(Path(td))
        print("\n[4] 重启续写")
        test_writer_resume(Path(td))

    srv.shutdown()

    print("\n" + "=" * 50)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())