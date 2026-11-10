"""边界与故障注入：把防御性分支、异常路径、降级逻辑都跑一遍。

这些用例大多在“线上真出问题”时才会被触发，所以必须用注入的方式验证它们
不会把进程带崩、不会静默丢数据。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap as B

record = B.record
media = B.media
writer = B.writer
config = B.config
ingestor = B.ingestor
main = B.main
Comp = B.Comp
CollectConfig = B.config.CollectConfig
Ingestor = B.ingestor.Ingestor


# ---------------------------------------------------------------- record 组件
class AllSegmentTypesTests(unittest.TestCase):
    def env(self, comps):
        return record.build_envelope(B.make_event(components=comps), CollectConfig(), 1)

    def test_face(self):
        seg = self.env([Comp.Face(id="178")]).record["msg"]["segments"][0]
        self.assertEqual(seg, {"t": "face", "id": "178"})

    def test_record(self):
        seg = self.env([Comp.Record(file="a.silk", url="http://x/a")]).record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "record")
        self.assertEqual(seg["url"], "http://x/a")

    def test_video(self):
        seg = self.env([Comp.Video(file="a.mp4", url="http://x/a")]).record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "video")

    def test_file(self):
        seg = self.env([Comp.File(name="a.txt", file="f", url="http://x/f")]).record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "file")
        self.assertEqual(seg["name"], "a.txt")

    def test_forward(self):
        seg = self.env([Comp.Forward(id="fw1")]).record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "forward")
        self.assertEqual(seg["id"], "fw1")

    def test_poke(self):
        seg = self.env([Comp.Poke(id="1")]).record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "poke")

    def test_dict_segments_all_types(self):
        comps = [
            {"type": "record", "data": {"file": "http://x/r.silk"}},
            {"type": "video", "data": {"url": "http://x/v.mp4"}},
            {"type": "file", "data": {"name": "n", "file": "http://x/f"}},
            {"type": "reply", "data": {"id": "r1"}},
            {"type": "face", "data": {"id": "9"}},
            {"type": "plain", "data": {"text": "p"}},
        ]
        segs = self.env(comps).record["msg"]["segments"]
        self.assertEqual([s["t"] for s in segs], ["record", "video", "file", "reply", "face", "text"])
        self.assertEqual(segs[0]["url"], "http://x/r.silk")
        self.assertEqual(segs[2]["url"], "http://x/f")
        self.assertEqual(segs[3]["id"], "r1")

    def test_non_iterable_chain(self):
        obj = B.SimpleNamespace(
            type=B.SimpleNamespace(name="GROUP_MESSAGE"),
            self_id="10001",
            message_id="m",
            group_id="123",
            sender=B.SimpleNamespace(user_id="1"),
            message=12345,
            message_str="x",
            raw_message={"message_type": "group", "group_id": "123"},
            timestamp=1756700000,
        )
        env = record.build_envelope(B.AstrMessageEvent(obj), CollectConfig(), 1)
        self.assertEqual(env.record["msg"]["segments"], [])
        self.assertEqual(env.record["msg"]["text"], "x")

    def test_object_without_dict_as_component(self):
        seg = self.env([42]).record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "unknown")
        self.assertEqual(seg["cls"], "int")

    def test_slotted_raw_falls_back_to_empty(self):
        class Slotted:
            __slots__ = ()

        obj = B.SimpleNamespace(
            type=B.SimpleNamespace(name="GROUP_MESSAGE"),
            self_id="10001",
            message_id="m",
            group_id="123",
            sender=B.SimpleNamespace(user_id="1"),
            message=[Comp.Plain("x")],
            message_str="x",
            raw_message=Slotted(),
            timestamp=1756700000,
        )
        env = record.build_envelope(B.AstrMessageEvent(obj), CollectConfig(), 1)
        self.assertEqual(env.record["conv"]["key"], "g/123")

    def test_attribute_that_raises(self):
        class Evil:
            name = "GROUP_MESSAGE"

            @property
            def timestamp(self):
                raise RuntimeError("boom")

        obj = B.SimpleNamespace(
            type=Evil(),
            self_id="10001",
            message_id="m",
            group_id="123",
            sender=Evil(),
            message=[Comp.Plain("x")],
            message_str="x",
            raw_message={"message_type": "group", "group_id": "123", "time": 1756700000},
        )
        env = record.build_envelope(B.AstrMessageEvent(obj), CollectConfig(), 1)
        self.assertEqual(env.record["ts"], 1756700000000)

    def test_notice_with_raw_stored(self):
        cfg = CollectConfig.from_mapping({"store_raw_event": True})
        ev = B.make_event(notice="group_recall", group_id="123", user_id="777")
        env = record.build_envelope(ev, cfg, 1)
        self.assertIn("raw", env.record)

    def test_scalars_skips_nested(self):
        ev = B.make_event(
            notice="group_ban",
            group_id="123",
            user_id="777",
            raw_extra={"nested": {"a": 1}, "list": [1, 2], "flag": True},
        )
        env = record.build_envelope(ev, CollectConfig(), 1)
        self.assertNotIn("nested", env.record["detail"])
        self.assertEqual(env.record["detail"]["flag"], True)


# ---------------------------------------------------------------- media 分支
class MediaBackendTests(unittest.TestCase):
    def test_sniff_webp_bmp_mp4(self):
        self.assertEqual(media._sniff(b"RIFF\x00\x00\x00\x00WEBPxxxx"), ("image/webp", ".webp"))
        self.assertEqual(media._sniff(b"BM" + b"\x00" * 10), ("image/bmp", ".bmp"))
        self.assertEqual(media._sniff(b"\x00\x00\x00\x18ftypmp42"), ("video/mp4", ".mp4"))

    def test_pick_backend_without_httpx(self):
        with mock.patch.dict(sys.modules, {"httpx": None}):
            got = media._pick_backend()
        self.assertIn(got, ("aiohttp", "urllib"))

    def test_pick_backend_without_any(self):
        with mock.patch.dict(sys.modules, {"httpx": None, "aiohttp": None}):
            self.assertEqual(media._pick_backend(), "urllib")


class MediaAiohttpTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = B.LocalHTTP()
        try:
            import aiohttp  # noqa: F401

            cls.has_aiohttp = True
        except ImportError:
            cls.has_aiohttp = False

    @classmethod
    def tearDownClass(cls):
        cls.http.close()

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-aio-")
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def store(self, **kw):
        st = media.MediaStore(root=self.root, max_bytes=kw.get("max_bytes", 1024 * 1024),
                              timeout=kw.get("timeout", 5.0), concurrency=2)
        st.backend = "aiohttp"
        return st

    async def test_aiohttp_ok_and_error(self):
        if not self.has_aiohttp:
            self.skipTest("没有 aiohttp")
        st = self.store()
        ok = await st.fetch(self.http.url("/png"))
        self.assertNotIn("err", ok)
        bad = await st.fetch(self.http.url("/nope"))
        self.assertIn("err", bad)

    async def test_aiohttp_too_large(self):
        if not self.has_aiohttp:
            self.skipTest("没有 aiohttp")
        st = self.store(max_bytes=1024)
        res = await st.fetch(self.http.url("/big"))
        self.assertEqual(res.get("err"), "too_large")


# ---------------------------------------------------------------- config 分支
class ConfigEdgeTests(unittest.TestCase):
    def test_bool_weird_type(self):
        self.assertFalse(config._as_bool(object(), False))
        self.assertTrue(config._as_bool(object(), True))

    def test_int_weird_type(self):
        self.assertEqual(config._as_int(object(), 7), 7)

    def test_list_tuple_and_set(self):
        self.assertEqual(config._as_list(("a", "b")), ["a", "b"])
        self.assertEqual(config._as_list({"a"}), ["a"])

    def test_list_other_type(self):
        self.assertEqual(config._as_list(123), [])

    def test_get_that_raises(self):
        class Bad:
            def get(self, *a, **k):
                raise RuntimeError("boom")

        c = CollectConfig.from_mapping(Bad())
        self.assertEqual(c.max_file_mb, 128)

    def test_home_dir_expanded(self):
        c = CollectConfig.from_mapping({"data_dir": "~"})
        self.assertTrue(c.resolve_root().is_absolute())
        self.assertNotIn("~", str(c.resolve_root()))


# ---------------------------------------------------------------- writer 故障
KB = 1024


def make_writer(root, **kw):
    kw.setdefault("max_file_bytes", 64 * KB)
    kw.setdefault("flush_interval", 0.02)
    kw.setdefault("fsync_interval", 0)
    return writer.ShardedWriter(root=Path(root), **kw)


class WriterFailureTests(unittest.TestCase):
    def test_stage_failure_is_counted_not_raised(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            original = w._stage

            def boom(conv_rel, day, data):
                raise RuntimeError("stage boom")

            w._stage = boom
            self.assertTrue(w.submit("g/1", "2025-01-01", "{}"))
            w.close()
            self.assertGreaterEqual(w.dropped, 1)
            w._stage = original

    def test_invalid_path_does_not_crash_thread(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w.submit("g/bad<>name", "2025-01-01", "{}")
            w.submit("g/1", "2025-01-01", '{"ok":1}')
            w.close()
            rows = B.read_jsonl(Path(td) / "g" / "1" / "2025-01-01.000.jsonl")
            self.assertEqual(rows, [{"ok": 1}])
            self.assertGreaterEqual(w.dropped, 1)

    def test_fsync_failure_swallowed(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td, fsync_interval=0.001)
            with mock.patch("os.fsync", side_effect=OSError("fsync boom")):
                w.submit("g/1", "2025-01-01", '{"a":1}')
                w.close()
            self.assertTrue((Path(td) / "g" / "1" / "2025-01-01.000.jsonl").exists())

    def test_close_timeout_drains_queue(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w._stop.set()
            w._thread.join(5)  # 先让真实线程退出

            class FakeThread:
                def is_alive(self):
                    return True

                def join(self, timeout=None):
                    return None

            w._thread = FakeThread()
            w.submit("g/1", "2025-01-01", '{"a":1}')
            w.close()
            rows = B.read_jsonl(Path(td) / "g" / "1" / "2025-01-01.000.jsonl")
            self.assertEqual(rows, [{"a": 1}])

    def test_manifest_close_failure_swallowed(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w.submit("g/1", "2025-01-01", '{"a":1}')
            w.close()

            class BadFH:
                def write(self, s):
                    return 0

                def flush(self):
                    return None

                def close(self):
                    raise OSError("close boom")

            w2 = make_writer(td)
            w2._manifest_fh = BadFH()
            w2.submit("g/1", "2025-01-02", '{"b":2}')
            w2.close()
            self.assertTrue((Path(td) / "g" / "1" / "2025-01-02.000.jsonl").exists())

    def test_handle_open_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w._closed = False
            with mock.patch("builtins.open", side_effect=OSError("open boom")):
                self.assertIsNone(w._handle(Path(td) / "g" / "x" / "a.jsonl"))
            w.close()

    def test_stage_error_while_draining(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w._stop.set()
            w._thread.join(5)

            class FakeThread:
                def is_alive(self):
                    return True

                def join(self, timeout=None):
                    return None

            w._thread = FakeThread()
            w._stage = mock.Mock(side_effect=RuntimeError("drain boom"))
            w.submit("g/1", "2025-01-01", "{}")
            w.close()  # 不能抛
            self.assertEqual(w._stage.call_count, 1)


# ---------------------------------------------------------------- ingestor 故障
class IngestorFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-edge-")
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

    async def test_start_twice(self):
        ing = await self.make()
        task = ing._task
        await ing.start()
        self.assertIs(ing._task, task)

    async def test_media_fetch_raises(self):
        ing = await self.make(download_media=True)
        assert ing.media is not None

        async def boom(url):
            raise RuntimeError("boom")

        ing.media.fetch = boom
        ing.offer(B.make_event(components=[Comp.Image(url="http://x/y.png")], msg_id="m1"))
        await asyncio.sleep(0.1)
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertIn("err", seg)
        self.assertGreaterEqual(ing.stats.get("media_error"), 1)

    async def test_media_fetch_returns_empty(self):
        ing = await self.make(download_media=True)

        async def none(url):
            return None

        ing.media.fetch = none  # type: ignore[union-attr]
        ing.offer(B.make_event(components=[Comp.Image(url="http://x/y.png")], msg_id="m1"))
        await asyncio.sleep(0.1)
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertEqual(seg.get("err"), "empty")

    async def test_image_without_url(self):
        ing = await self.make(download_media=True)
        ing.offer(B.make_event(components=[Comp.Image(file="abc.image")], msg_id="m1"))
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertEqual(seg.get("err"), "no_url")

    async def test_local_file_ref_used_when_no_url(self):
        ing = await self.make(download_media=True)
        ing.offer(
            B.make_event(components=[Comp.Image(file="http://127.0.0.1:1/x.png")], msg_id="m1")
        )
        await ing.aclose()
        seg = B.all_records(self.root)[0]["msg"]["segments"][0]
        self.assertIn("err", seg)  # 连不上，但走了下载流程

    async def test_writer_submit_failure_counted(self):
        ing = await self.make()
        ing.writer.submit = mock.Mock(side_effect=RuntimeError("writer boom"))
        ing.offer(B.make_event(text="x", msg_id="m1"))
        await asyncio.sleep(0.1)
        self.assertGreaterEqual(ing.stats.get("write_error"), 1)

    async def test_stats_logging_path(self):
        ing = await self.make(log_stats_interval_s=1)
        ing.logger = B.stub_logger
        ing._last_stats = 0  # 强制到期
        ing.offer(B.make_event(text="x", msg_id="m1"))
        await asyncio.sleep(0.15)
        self.assertTrue(any("stats:" in msg for _, msg in B.stub_logger.records))

    async def test_notice_without_time(self):
        ing = await self.make()
        ev = B.make_event(notice="group_recall", group_id="123", user_id="777")
        del ev.message_obj.raw_message["time"]
        ing.offer(ev)
        await ing.aclose()
        rows = B.all_records(self.root)
        self.assertEqual(rows[0]["type"], "notice")


# ---------------------------------------------------------------- main 兜底
class MainFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_module_loaded_without_package(self):
        spec = importlib.util.spec_from_file_location(
            "trainlog_standalone_main", B.PLUGIN_DIR / "main.py"
        )
        self.assertIsNotNone(spec)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["trainlog_standalone_main"] = mod
        try:
            spec.loader.exec_module(mod)  # 相对导入失败 -> 走 sys.path 兜底
            self.assertTrue(hasattr(mod, "TrainLogPlugin"))
        finally:
            sys.modules.pop("trainlog_standalone_main", None)

    async def test_star_init_typeerror_fallback(self):
        original = B.main.Star.__init__

        def fake_init(self, context=None):
            if context is not None:
                raise TypeError("old signature")

        B.main.Star.__init__ = fake_init
        try:
            p = B.main.TrainLogPlugin(context=object(), config={"enabled": False})
            self.assertFalse(p.cfg.enabled)
        finally:
            B.main.Star.__init__ = original

    async def test_terminate_swallows_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = B.main.TrainLogPlugin(
                context=None,
                config={"data_dir": td, "log_stats_interval_s": 0, "fsync_interval_s": 0},
            )
            await p.on_all_message(B.make_event(text="x", msg_id="m1"))
            ing = p._ingestor

            async def boom():
                raise RuntimeError("close boom")

            ing.aclose = boom
            await p.terminate()  # 不应抛
            self.assertIsNone(p._ingestor)
            ing.writer.close()  # 兜底：锁文件要关掉，否则 Windows 删不掉临时目录


if __name__ == "__main__":
    unittest.main()