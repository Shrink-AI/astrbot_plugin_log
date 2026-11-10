"""采集管线：过滤、去重、背压、媒体开关、生命周期。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import _bootstrap as B

CollectConfig = B.config.CollectConfig
Ingestor = B.ingestor.Ingestor
Comp = B.Comp


class IngestorBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-ing-")
        self.root = Path(self._td.name)
        self._made = []

    async def asyncTearDown(self):
        for ing in self._made:
            try:
                await ing.aclose()
            except Exception:
                pass
        self._td.cleanup()

    def cfg(self, **kw):
        base = {
            "data_dir": str(self.root),
            "log_stats_interval_s": 0,
            "fsync_interval_s": 0,
        }
        base.update(kw)
        return CollectConfig.from_mapping(base)

    async def make(self, **kw):
        ing = Ingestor(self.cfg(**kw))
        await ing.start()
        self._made.append(ing)
        return ing

    def records(self):
        return B.all_records(self.root)


class BasicTests(IngestorBase):
    async def test_offer_before_start(self):
        ing = Ingestor(self.cfg())
        self.assertFalse(ing.offer(B.make_event(text="x")))
        ing.writer.close()

    async def test_basic_write(self):
        ing = await self.make()
        self.assertTrue(ing.offer(B.make_event(text="hello", msg_id="m1")))
        await ing.aclose()
        rows = self.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["msg"]["text"], "hello")

    async def test_garbage_event_ignored(self):
        ing = await self.make()
        self.assertFalse(ing.offer(B.SimpleNamespace()))
        self.assertEqual(ing.stats.get("ignored"), 1)

    async def test_parse_error_counted(self):
        ing = await self.make()
        original = B.ingestor.build_envelope

        def boom(*a, **k):
            raise RuntimeError("boom")

        B.ingestor.build_envelope = boom
        try:
            self.assertFalse(ing.offer(B.make_event(text="x")))
        finally:
            B.ingestor.build_envelope = original
        self.assertEqual(ing.stats.get("parse_error"), 1)

    async def test_stats_counters(self):
        ing = await self.make()
        for i in range(3):
            ing.offer(B.make_event(text=f"m{i}", msg_id=f"m{i}"))
        await ing.aclose()
        self.assertEqual(ing.stats.get("received"), 3)
        self.assertEqual(ing.stats.get("written"), 3)

    async def test_aclose_idempotent(self):
        ing = await self.make()
        ing.offer(B.make_event(text="x"))
        await ing.aclose()
        await ing.aclose()
        self.assertEqual(len(self.records()), 1)


class FilterTests(IngestorBase):
    async def test_ignore_self(self):
        ing = await self.make(ignore_self=True)
        self.assertFalse(ing.offer(B.make_event(user_id="10001", text="bot")))
        self.assertTrue(ing.offer(B.make_event(user_id="111", text="human")))
        await ing.aclose()
        rows = self.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sender"]["id"], "111")

    async def test_group_blacklist(self):
        ing = await self.make(group_blacklist=["999"])
        self.assertFalse(ing.offer(B.make_event(group_id="999", text="x")))
        self.assertTrue(ing.offer(B.make_event(group_id="111", text="x")))
        await ing.aclose()
        self.assertEqual(len(self.records()), 1)

    async def test_group_whitelist(self):
        ing = await self.make(group_whitelist=["111"])
        self.assertFalse(ing.offer(B.make_event(group_id="222", text="x")))
        self.assertTrue(ing.offer(B.make_event(group_id="111", text="x")))
        await ing.aclose()
        self.assertEqual(len(self.records()), 1)

    async def test_collect_private_off(self):
        ing = await self.make(collect_private=False)
        self.assertFalse(ing.offer(B.make_event(message_type="private", group_id="", text="x")))
        self.assertTrue(ing.offer(B.make_event(group_id="111", text="x")))
        await ing.aclose()
        rows = self.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conv"]["kind"], "group")

    async def test_collect_group_off(self):
        ing = await self.make(collect_group=False)
        self.assertFalse(ing.offer(B.make_event(group_id="111", text="x")))
        self.assertTrue(ing.offer(B.make_event(message_type="private", group_id="", text="x")))
        await ing.aclose()
        self.assertEqual(self.records()[0]["conv"]["kind"], "private")

    async def test_temp_session_off(self):
        ing = await self.make(collect_temp_session=False)
        ev = B.make_event(message_type="private", group_id="555", user_id="222", sub_type="group", text="x")
        self.assertFalse(ing.offer(ev))
        await ing.aclose()
        self.assertEqual(self.records(), [])

    async def test_notice_not_filtered_by_self(self):
        ing = await self.make(ignore_self=True)
        ev = B.make_event(notice="group_recall", group_id="111", user_id="10001")
        self.assertTrue(ing.offer(ev))
        await ing.aclose()
        self.assertEqual(self.records()[0]["type"], "notice")


class DedupeTests(IngestorBase):
    async def test_same_message_dropped(self):
        ing = await self.make()
        self.assertTrue(ing.offer(B.make_event(text="x", msg_id="same")))
        self.assertFalse(ing.offer(B.make_event(text="x", msg_id="same")))
        await ing.aclose()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(ing.stats.get("duplicated"), 1)

    async def test_same_msg_id_different_conv_kept(self):
        ing = await self.make()
        self.assertTrue(ing.offer(B.make_event(group_id="111", text="x", msg_id="1")))
        self.assertTrue(ing.offer(B.make_event(group_id="222", text="x", msg_id="1")))
        await ing.aclose()
        self.assertEqual(len(self.records()), 2)

    async def test_same_msg_id_different_bot_kept(self):
        ing = await self.make()
        self.assertTrue(ing.offer(B.make_event(text="x", msg_id="1", self_id="10001")))
        self.assertTrue(ing.offer(B.make_event(text="x", msg_id="1", self_id="10002")))
        await ing.aclose()
        self.assertEqual(len(self.records()), 2)


class BackpressureTests(IngestorBase):
    async def test_queue_full_drops_without_raise(self):
        ing = await self.make(queue_size=64)
        ing._task.cancel()
        try:
            await ing._task
        except asyncio.CancelledError:
            pass
        ok = 0
        for i in range(200):
            if ing.offer(B.make_event(text="x", msg_id=f"m{i}")):
                ok += 1
        self.assertEqual(ok, 64)
        self.assertGreaterEqual(ing.stats.get("dropped_queue"), 1)


class MediaSwitchTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = B.LocalHTTP()

    @classmethod
    def tearDownClass(cls):
        cls.http.close()

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-med-")
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
            "log_stats_interval_s": 0,
            "fsync_interval_s": 0,
            "media_timeout_s": 5,
        }
        base.update(kw)
        ing = Ingestor(CollectConfig.from_mapping(base))
        await ing.start()
        self._made.append(ing)
        return ing

    def image_event(self, msg_id="m1"):
        return B.make_event(components=[Comp.Image(url=self.http.url("/png"))], msg_id=msg_id)

    async def test_media_downloaded(self):
        ing = await self.make(download_media=True, media_kinds=["image"])
        ing.offer(self.image_event())
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertIn("sha256", seg)
        self.assertTrue((self.root / seg["path"]).exists())

    async def test_media_disabled_keeps_url(self):
        ing = await self.make(download_media=False)
        ing.offer(self.image_event())
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertNotIn("sha256", seg)
        self.assertTrue(seg["url"].startswith("http://"))

    async def test_media_kinds_empty_no_download(self):
        ing = await self.make(download_media=True, media_kinds=[])
        ing.offer(self.image_event())
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertNotIn("sha256", seg)

    async def test_media_kinds_excludes_image(self):
        ing = await self.make(download_media=True, media_kinds=["record"])
        ing.offer(self.image_event())
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertNotIn("sha256", seg)

    async def test_media_failure_recorded(self):
        ing = await self.make(download_media=True, media_kinds=["image"])
        ev = B.make_event(components=[Comp.Image(url=self.http.url("/404img"))], msg_id="m2")
        ing.offer(ev)
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertIn("err", seg)
        self.assertGreaterEqual(ing.stats.get("media_error"), 1)


if __name__ == "__main__":
    unittest.main()