"""再加几轮：随机 fuzz、长跑、多实例同目录、编码边界、工具与配置一致性。

这些不是"某个函数的单元测试"，而是对整个落盘产物做体检：
凡是写出来的文件，必须每一行都是严格 JSON、都能被下游 pandas 直接读。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap as B

record = B.record
writer = B.writer
config = B.config
CollectConfig = B.config.CollectConfig
Ingestor = B.ingestor.Ingestor
Comp = B.Comp

ROOT = B.PLUGIN_DIR


# ------------------------------------------------------------------ 随机事件
TEXTS = [
    "",
    " ",
    "\n\n",
    "\x00",
    "a" * 5000,
    "😀" * 40,
    "\ud800",  # 孤立代理项
    "👨‍👩‍👧‍👦",
    "ｆｕｌｌｗｉｄｔｈ",
    '"quoted" \\ backslash',
    "<script>alert(1)</script>",
    "\u202eRTL",
]


def rand_scalar(rnd):
    return rnd.choice(
        [None, True, False, 0, -1, 2**31, 1.5, "", "x", 10**20, [], {}, {"a": 1}, (1, 2)]
    )


def rand_component(rnd):
    pick = rnd.randint(0, 13)
    if pick == 0:
        return Comp.Plain(rnd.choice(TEXTS))
    if pick == 1:
        return Comp.Image(url=rnd.choice(["http://x/a.png", "", None]), file="f")
    if pick == 2:
        return Comp.At(qq=rnd.choice(["123", "", "all"]))
    if pick == 3:
        return Comp.AtAll()
    if pick == 4:
        return Comp.Reply(id=rnd.choice(["1", ""]))
    if pick == 5:
        return Comp.Face(id="7")
    if pick == 6:
        return Comp.Record(file="a.silk", url="http://x/r")
    if pick == 7:
        return Comp.Video(file="a.mp4", url="")
    if pick == 8:
        return Comp.File(name="n", file="f", url="")
    if pick == 9:
        return Comp.Forward(id="fw")
    if pick == 10:
        return {"type": rnd.choice(["text", "image", "record", "reply", "video", "weird"]),
                "data": {"text": "t", "url": "http://x/i", "id": "1"}}
    if pick == 11:
        return rnd.choice([42, None, object(), 3.14])
    if pick == 12:
        return {"type": None, "data": None}
    return Comp.Plain("")


def rand_event(rnd):
    kind = rnd.choice(["group", "group", "private", "temp", "notice", "notice", "garbage"])
    comps = [rand_component(rnd) for _ in range(rnd.randint(0, 4))]
    self_id = rnd.choice(["10001", "10002", "", None])
    user = rnd.choice(["111", "222", "999999999999", "", None])
    gid = rnd.choice(["123456789", "987", "", None])
    ts = rnd.choice([1756700000, 1756700000123, 0, -5, 10**10, None])

    raw = {}
    if kind == "group":
        raw = {"message_type": "group", "group_id": gid, "user_id": user}
    elif kind == "private":
        raw = {"message_type": "private", "user_id": user}
        gid = ""
    elif kind == "temp":
        raw = {"message_type": "private", "sub_type": "group", "group_id": gid, "user_id": user}
    elif kind == "notice":
        raw = {"post_type": "notice", "notice_type": rnd.choice(["group_recall", "group_ban", "x"]),
               "group_id": gid, "user_id": user}
    else:
        raw = rnd.choice([{}, {"message_type": 5}, {"post_type": "meta_event"}])
    if ts is not None:
        raw["time"] = ts

    obj = SimpleNamespace(
        type=SimpleNamespace(name=rnd.choice(["GROUP_MESSAGE", "PRIVATE_MESSAGE", "WEIRD"])),
        self_id=self_id,
        session_id="s",
        message_id=rnd.choice(["m1", "", None, "0"]),
        group_id=gid,
        sender=rnd.choice([SimpleNamespace(user_id=user, nickname="n"), None, object()]),
        message=comps,
        message_str=rnd.choice(TEXTS),
        raw_message=raw,
        timestamp=ts,
    )
    return B.AstrMessageEvent(obj)


class FuzzTests(unittest.TestCase):
    def test_fuzz_build_envelope(self):
        rnd = random.Random(20260101)
        cfg = CollectConfig()
        for i in range(3000):
            env = record.build_envelope(rand_event(rnd), cfg, i)
            if env is None:
                continue
            line = json.dumps(env.record, ensure_ascii=False)
            self.assertNotIn("NaN", line)
            back = json.loads(line)
            self.assertEqual(back["v"], 1)
            self.assertIsInstance(back["ts"], int)
            self.assertEqual(back["day"], B.session.day_of(back["ts"], cfg.timezone))
            self.assertTrue(back["conv"]["key"])
            self.assertTrue(env.conv_rel)

    def test_fuzz_never_produces_bad_key(self):
        rnd = random.Random(7)
        cfg = CollectConfig()
        for i in range(1500):
            env = record.build_envelope(rand_event(rnd), cfg, i)
            if env is None:
                continue
            key = env.record["conv"]["key"]
            # 目录里不能出现路径分隔符以外的奇怪东西
            self.assertNotIn("..", key)
            self.assertEqual(key, env.conv_rel)
            self.assertTrue(key.startswith(("g/", "p/", "t/")), key)


class EncodingEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-enc-")
        self.root = Path(self._td.name)

    async def asyncTearDown(self):
        self._td.cleanup()

    async def run_events(self, events):
        ing = Ingestor(CollectConfig.from_mapping({
            "data_dir": str(self.root), "log_stats_interval_s": 0, "fsync_interval_s": 0,
        }), B.stub_logger)
        await ing.start()
        for ev in events:
            ing.offer(ev)
        await ing.aclose()
        return ing

    async def test_lone_surrogate_message_not_dropped(self):
        ev = B.make_event(text="前\ud800后")
        ing = await self.run_events([ev])
        rows = B.all_records(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(ing.stats.get("written"), 1)
        self.assertEqual(ing.stats.get("write_error"), 0)
        self.assertEqual(rows[0]["msg"]["text"], "前\ud800后")

    async def test_files_are_strict_utf8_and_newline_terminated(self):
        events = [B.make_event(text=t, msg_id=f"m{i}") for i, t in enumerate(TEXTS)]
        await self.run_events(events)
        for f in B.all_files(self.root):
            raw = f.read_bytes()
            self.assertTrue(raw.endswith(b"\n"), f.name)
            text = raw.decode("utf-8")  # 必须是合法 UTF-8
            for line in text.splitlines():
                json.loads(line)  # 必须每行都是严格 JSON


class MultiInstanceTests(unittest.IsolatedAsyncioTestCase):
    """两个实例共用同一个 data_dir（同机跑两个 bot、或旧进程没退干净）。

    实测这种用法会让两个写入者交替 append 同一文件，大块写入被 OS 拆开后
    互相穿插，1%~3% 概率静默丢记录。所以插件加了目录独占锁：第二个实例
    明确拒绝写入并报错，而不是把数据写坏。
    """

    async def test_second_instance_refuses_to_write(self):
        with tempfile.TemporaryDirectory(prefix="trainlog-mi-") as td:
            root = Path(td)
            mk = lambda: Ingestor(CollectConfig.from_mapping({
                "data_dir": td, "max_file_mb": 4,
                "log_stats_interval_s": 0, "fsync_interval_s": 0,
            }), B.stub_logger)

            a, b = mk(), mk()
            await a.start()
            await b.start()
            try:
                self.assertTrue(a.writer.locked)
                self.assertFalse(b.writer.locked)
                self.assertIsNone(b.writer._thread)

                for i in range(300):
                    a.offer(B.make_event(group_id="555", user_id="1", text=f"a{i}",
                                         msg_id=f"a{i}", ts=1756700000000 + i))
                    b.offer(B.make_event(group_id="555", user_id="1", text=f"b{i}",
                                         msg_id=f"b{i}", ts=1756700001000 + i))
                    if i % 50 == 0:
                        await asyncio.sleep(0.005)

                await b.aclose()
                await a.aclose()

                rows = []
                for f in B.all_files(root):
                    rows.extend(B.read_jsonl(f))  # 任何半行都会在这里抛
                self.assertEqual(len(rows), 300)
                self.assertTrue(all(r["msg_id"].startswith("a") for r in rows))
                self.assertGreaterEqual(b.writer.dropped, 300)
                self.assertTrue(
                    any("另一个实例占用" in msg for _, msg in B.stub_logger.records)
                )
            finally:
                await b.aclose()
                await a.aclose()

    async def test_lock_released_after_close(self):
        with tempfile.TemporaryDirectory(prefix="trainlog-mi2-") as td:
            root = Path(td)
            mk = lambda: Ingestor(CollectConfig.from_mapping({
                "data_dir": td, "log_stats_interval_s": 0, "fsync_interval_s": 0,
            }), B.stub_logger)

            first = mk()
            await first.start()
            self.assertTrue(first.writer.locked)
            first.offer(B.make_event(text="one", msg_id="m1"))
            await first.aclose()

            second = mk()  # 前一个退出后必须能拿到锁
            await second.start()
            try:
                self.assertTrue(second.writer.locked)
                second.offer(B.make_event(text="two", msg_id="m2"))
            finally:
                await second.aclose()
            self.assertEqual(len(B.all_records(root)), 2)

    async def test_takeover_after_old_instance_quits(self):
        """热重载：新实例启动时旧实例还在，旧实例退出后应自动接管。"""
        with tempfile.TemporaryDirectory(prefix="trainlog-mi3-") as td:
            root = Path(td)
            mk = lambda: Ingestor(CollectConfig.from_mapping({
                "data_dir": td, "log_stats_interval_s": 0, "fsync_interval_s": 0,
            }), B.stub_logger)

            old = mk()
            await old.start()
            new = mk()
            await new.start()
            try:
                self.assertFalse(new.writer.locked)
                new.offer(B.make_event(text="early", msg_id="m1"))
                await asyncio.sleep(0.05)
                self.assertEqual(len(B.all_records(root)), 0)  # 被锁在外面，丢弃

                await old.aclose()
                new._lock_retry_at = 0.0  # 不等 30 秒
                new.offer(B.make_event(text="late", msg_id="m2"))
                await asyncio.sleep(0.2)
                self.assertTrue(new.writer.locked)
                self.assertEqual([r["msg_id"] for r in B.all_records(root)], ["m2"])
            finally:
                await new.aclose()
                await old.aclose()


class SoakTests(unittest.IsolatedAsyncioTestCase):
    async def test_20k_messages(self):
        with tempfile.TemporaryDirectory(prefix="trainlog-soak-") as td:
            root = Path(td)
            ing = Ingestor(CollectConfig.from_mapping({
                "data_dir": td, "max_file_mb": 1, "download_media": False,
                "queue_size": 5000, "log_stats_interval_s": 0, "fsync_interval_s": 1,
            }), B.stub_logger)
            await ing.start()
            n = 20000
            base = 1756700000000
            for i in range(n):
                gid = "111" if i % 2 else "222"
                ing.offer(B.make_event(group_id=gid, user_id=str(1000 + i % 7),
                                       text=f"第{i}条 消息内容 ok",
                                       msg_id=f"m{i}", ts=base + i * 1000))
                if i % 200 == 0:
                    await asyncio.sleep(0.001)
            await ing.aclose()

            self.assertEqual(ing.stats.get("written"), n)
            self.assertEqual(ing.stats.get("dropped_queue"), 0)
            self.assertEqual(ing.stats.get("write_error"), 0)

            files = B.all_files(root)
            total = 0
            for f in files:
                self.assertLessEqual(f.stat().st_size, 1024 * 1024, f.name)
                total += len(B.read_jsonl(f))
            self.assertEqual(total, n)

            manifest = B.read_jsonl(root / "_manifest.jsonl")
            self.assertEqual(len(manifest), len(files))
            self.assertEqual(sorted(m["path"] for m in manifest),
                             sorted(f.relative_to(root).as_posix() for f in files))
            self.assertEqual([p.name for p in root.rglob("*.tmp")], [])


class PluginRaceTests(unittest.IsolatedAsyncioTestCase):
    """插件外壳在并发回调、以及"刚写完就卸载"时的行为。"""

    async def test_concurrent_handler_calls_single_ingestor(self):
        with tempfile.TemporaryDirectory(prefix="trainlog-race-") as td:
            p = B.main.TrainLogPlugin(context=None, config={
                "data_dir": td, "log_stats_interval_s": 0, "fsync_interval_s": 0,
            })
            await asyncio.gather(*[
                p.on_all_message(B.make_event(text=f"并发{i}", msg_id=f"m{i}"))
                for i in range(50)
            ])
            await p.terminate()
            self.assertEqual(len(B.all_records(Path(td))), 50)

    async def test_terminate_flushes_inflight(self):
        with tempfile.TemporaryDirectory(prefix="trainlog-flush-") as td:
            ing = Ingestor(CollectConfig.from_mapping({
                "data_dir": td, "log_stats_interval_s": 0, "fsync_interval_s": 0,
            }), B.stub_logger)
            await ing.start()
            for i in range(500):
                ing.offer(B.make_event(text=f"m{i}", msg_id=f"m{i}", ts=1756700000000 + i))
            await ing.aclose()  # 不等消费完就关，也不能丢
            self.assertEqual(len(B.all_records(Path(td))), 500)


class ArtifactContractTests(unittest.TestCase):
    """配置/元数据/工具与代码本身的一致性。"""

    def test_schema_covers_every_field(self):
        schema = json.loads((B.PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        fields = {f.name for f in dataclasses.fields(CollectConfig)}
        self.assertEqual(set(schema), fields)

        for key, item in schema.items():
            self.assertIn(item["type"], ("bool", "int", "string", "list", "text"), key)
            self.assertTrue(item.get("description"), key)

    def test_schema_defaults_roundtrip(self):
        schema = json.loads((B.PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        cfg = CollectConfig.from_mapping({k: v["default"] for k, v in schema.items()})
        for key, item in schema.items():
            self.assertEqual(getattr(cfg, key), item["default"], key)

    def test_metadata_sane(self):
        text = (B.PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("name: astrbot_plugin_log", text)
        self.assertTrue(re.search(r"^version: \d+\.\d+\.\d+$", text, re.M))
        self.assertTrue(re.search(r"^astrbot_version: ", text, re.M))
        self.assertIn("aiocqhttp", text)

    def test_no_stray_files_in_plugin(self):
        names = {p.name for p in B.PLUGIN_DIR.iterdir()}
        self.assertIn("main.py", names)
        self.assertIn("metadata.yaml", names)
        self.assertIn("_conf_schema.json", names)
        for junk in (".coverage", "data", "logs"):
            self.assertNotIn(junk, names)


class InspectToolTests(unittest.TestCase):
    def run_tool(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "tools" / "inspect.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), env=env, timeout=120,
        )

    def test_missing_dir(self):
        r = self.run_tool(str(Path(tempfile.gettempdir()) / "definitely-not-here-xyz"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("目录不存在", r.stdout)

    def test_reports_real_data(self):
        with tempfile.TemporaryDirectory(prefix="trainlog-tool-") as td:
            root = Path(td)
            d = root / "g" / "123456789"
            d.mkdir(parents=True)
            rows = [
                {"v": 1, "type": "message", "day": "2025-09-01", "ts": 1756700000000},
                {"v": 1, "type": "message", "day": "2025-09-01", "ts": 1756700005000},
            ]
            (d / "2025-09-01.000.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            (d / "2025-09-01.001.jsonl").write_text("{bad json}\n\n", encoding="utf-8")
            media = root / "media" / "ab"
            media.mkdir(parents=True)
            (media / "x.png").write_bytes(b"12345")

            r = self.run_tool(str(root))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("g/123456789", r.stdout)
            self.assertIn("合计", r.stdout)
            # 坏行只影响时间范围解析，不影响计数
            self.assertIn("3", r.stdout)
            self.assertIn("媒体: 1 个文件", r.stdout)

            r2 = self.run_tool(str(root), "--no-time")
            self.assertEqual(r2.returncode, 0, r2.stderr)


if __name__ == "__main__":
    unittest.main()