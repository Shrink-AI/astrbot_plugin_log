"""端到端：多会话混流、跨天分片、媒体去重、重启续写、数据完整性。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _bootstrap as B

CollectConfig = B.config.CollectConfig
Ingestor = B.ingestor.Ingestor
Comp = B.Comp

REQUIRED = ("v", "type", "day", "ts", "recv_ts", "self_id", "msg_id", "conv", "sender", "msg")


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = B.LocalHTTP()

    @classmethod
    def tearDownClass(cls):
        cls.http.close()

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-e2e-")
        self.root = Path(self._td.name)
        self._made = []

    async def asyncTearDown(self):
        for ing in self._made:
            try:
                await ing.aclose()
            except Exception:
                pass
        self._td.cleanup()

    async def make(self, **kw):
        base = {
            "data_dir": str(self.root),
            "max_file_mb": 1,
            "log_stats_interval_s": 0,
            "fsync_interval_s": 0,
            "queue_size": 20000,
        }
        base.update(kw)
        ing = Ingestor(CollectConfig.from_mapping(base))
        await ing.start()
        self._made.append(ing)
        return ing

    async def test_multi_conversation_split(self):
        ing = await self.make()
        base = 1756700000000
        n = 100
        for i in range(n):
            ing.offer(B.make_event(group_id="111", user_id="1", text=f"a{i}", msg_id=f"a{i}", ts=base + i))
            ing.offer(B.make_event(group_id="222", user_id="2", text=f"b{i}", msg_id=f"b{i}", ts=base + i))
            ing.offer(
                B.make_event(
                    message_type="private", group_id="", user_id="777",
                    text=f"c{i}", msg_id=f"c{i}", ts=base + i,
                )
            )
        await ing.aclose()

        self.assertGreater(len(B.all_files(self.root)), 0)
        rows = B.all_records(self.root)
        self.assertEqual(len(rows), 3 * n)

        by_conv = {}
        for r in rows:
            by_conv.setdefault(r["conv"]["key"], []).append(r)
        self.assertEqual(len(by_conv), 3)
        self.assertEqual(len(by_conv["g/111"]), n)
        self.assertEqual(len(by_conv["g/222"]), n)
        self.assertEqual(len(by_conv["p/777_10001"]), n)
        for key, items in by_conv.items():
            self.assertEqual(len({r["msg_id"] for r in items}), n, key)

    async def test_day_split(self):
        ing = await self.make()
        day1_ts = 1756700000000
        day2_ts = day1_ts + 48 * 3600 * 1000
        ing.offer(B.make_event(text="d1", msg_id="d1", ts=day1_ts))
        ing.offer(B.make_event(text="d2", msg_id="d2", ts=day2_ts))
        await ing.aclose()

        d1 = B.session.day_of(day1_ts, "Asia/Shanghai")
        d2 = B.session.day_of(day2_ts, "Asia/Shanghai")
        self.assertNotEqual(d1, d2)
        d = self.root / "g" / "123456789"
        self.assertTrue((d / f"{d1}.000.jsonl").exists())
        self.assertTrue((d / f"{d2}.000.jsonl").exists())

    async def test_records_all_valid(self):
        ing = await self.make()
        for i in range(50):
            comps = [Comp.Plain(f"文本{i}")]
            if i % 5 == 0:
                comps.append(Comp.Image(url=self.http.url("/png")))
            if i % 7 == 0:
                comps.append(Comp.At(qq="222"))
            ing.offer(B.make_event(components=comps, msg_id=f"m{i}", ts=1756700000000 + i))
        await ing.aclose()

        rows = B.all_records(self.root)
        self.assertEqual(len(rows), 50)
        for r in rows:
            for key in REQUIRED:
                self.assertIn(key, r)
            self.assertEqual(r["v"], 1)
            self.assertEqual(r["type"], "message")
            self.assertIsInstance(r["ts"], int)
            self.assertEqual(r["day"], B.session.day_of(r["ts"], "Asia/Shanghai"))
            self.assertTrue(r["conv"]["key"])

    async def test_media_deduped_across_messages(self):
        ing = await self.make(media_kinds=["image"])
        for i in range(10):
            ing.offer(
                B.make_event(
                    components=[Comp.Image(url=self.http.url("/png"))],
                    msg_id=f"img{i}", ts=1756700000000 + i,
                )
            )
        await ing.aclose()
        media_files = [p for p in (self.root / "media").rglob("*") if p.is_file()]
        self.assertEqual(len(media_files), 1)
        segs = [s for r in B.all_records(self.root) for s in r["msg"]["segments"] if s["t"] == "image"]
        self.assertEqual(len(segs), 10)
        self.assertEqual({s["sha256"] for s in segs}, {segs[0]["sha256"]})

    async def test_notice_and_message_same_file(self):
        ing = await self.make()
        base = 1756700000000
        ing.offer(B.make_event(text="说一句", msg_id="m1", ts=base))
        ing.offer(B.make_event(notice="group_recall", msg_id="n1", ts=base + 1000))
        ing.offer(B.make_event(text="再说一句", msg_id="m2", ts=base + 2000))
        await ing.aclose()

        day = B.session.day_of(base, "Asia/Shanghai")
        f = self.root / "g" / "123456789" / f"{day}.000.jsonl"
        rows = B.read_jsonl(f)
        self.assertEqual([r["type"] for r in rows], ["message", "notice", "message"])

    async def test_restart_continues_same_part(self):
        base = 1756700000000
        first = await self.make()

        async def feed(ing, lo, hi):
            for i in range(lo, hi):
                ing.offer(B.make_event(text=f"m{i}", msg_id=f"m{i}", ts=base + i))

        await feed(first, 0, 100)
        await first.aclose()

        second = Ingestor(CollectConfig.from_mapping({
            "data_dir": str(self.root),
            "max_file_mb": 1,
            "log_stats_interval_s": 0,
            "fsync_interval_s": 0,
        }))
        await second.start()
        self._made.append(second)
        await feed(second, 100, 200)
        await second.aclose()

        day = B.session.day_of(base, "Asia/Shanghai")
        d = self.root / "g" / "123456789"
        parts = sorted(d.glob(f"{day}.*.jsonl"))
        self.assertEqual(len(parts), 1, [p.name for p in parts])
        rows = B.read_jsonl(parts[0])
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({r["msg_id"] for r in rows}), 200)

    async def test_dirs_created(self):
        ing = await self.make()
        ing.offer(B.make_event(components=[Comp.Image(url=self.http.url("/png"))], msg_id="m1"))
        await ing.aclose()
        self.assertTrue((self.root / "g").is_dir())
        self.assertTrue((self.root / "media").is_dir())
        self.assertTrue((self.root / "_manifest.jsonl").exists())

    async def test_manifest_matches_files(self):
        ing = await self.make()
        for i in range(5):
            ing.offer(B.make_event(text="x", msg_id=f"m{i}", ts=1756700000000 + i))
        await ing.aclose()
        manifest = B.read_jsonl(self.root / "_manifest.jsonl")
        files = B.all_files(self.root)
        self.assertEqual(sorted(m["path"] for m in manifest), sorted(f.relative_to(self.root).as_posix() for f in files))


if __name__ == "__main__":
    unittest.main()