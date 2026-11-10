"""最后一层覆盖：那些只有出故障时才会走到的分支。

目的不是刷覆盖率数字，而是确认「坏了的时候」不会崩、不会静默丢数据。
"""

from __future__ import annotations

import asyncio
import errno
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _bootstrap as B

record = B.record
writer = B.writer
config = B.config
Ingestor = B.ingestor.Ingestor
CollectConfig = B.config.CollectConfig
Comp = B.Comp

KB = 1024


def make_writer(root, **kw):
    kw.setdefault("max_file_bytes", 64 * KB)
    kw.setdefault("flush_interval", 0.02)
    kw.setdefault("fsync_interval", 0)
    kw.setdefault("logger", B.stub_logger)
    return writer.ShardedWriter(root=Path(root), **kw)


class BadHandle:
    """写入即失败的文件句柄。"""

    def write(self, data):
        raise OSError("write boom")

    def flush(self):
        return None

    def fileno(self):
        return -1

    def close(self):
        return None


class CloseFailingHandle(BadHandle):
    def write(self, data):
        return len(data)

    def close(self):
        raise OSError("close boom")


class WriterDeepTests(unittest.TestCase):
    def test_day_roll_skips_full_parts(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            full = d / "2025-01-02.000.jsonl"
            full.write_text("x" * (70 * KB) + "\n", encoding="utf-8")

            w = make_writer(td)
            w.submit("g/1", "2025-01-01", "{}")
            w.submit("g/1", "2025-01-02", '{"a":1}')
            w.close()

            self.assertTrue((d / "2025-01-02.001.jsonl").exists())
            self.assertEqual(B.read_jsonl(d / "2025-01-02.001.jsonl"), [{"a": 1}])

    def test_flush_drops_when_handle_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w._handle = lambda path: None
            w.submit("g/1", "2025-01-01", "{}")
            w.close()
            self.assertGreaterEqual(w.dropped, 1)

    def test_flush_write_error_counted(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w._handle = lambda path: BadHandle()
            w.submit("g/1", "2025-01-01", "{}")
            w.close()
            self.assertGreaterEqual(w.dropped, 1)

    def test_lru_eviction_close_error(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td, max_open_files=8)
            for i in range(8):
                w._handles[Path(td) / f"x{i}"] = CloseFailingHandle()
            fh = w._handle(Path(td) / "g" / "1" / "a.jsonl")
            self.assertIsNotNone(fh)
            w.close()

    def test_close_handles_ignores_errors(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            w._handles[Path(td) / "weird"] = CloseFailingHandle()
            w.close()  # 不应抛

    def test_manifest_write_error(self):
        with tempfile.TemporaryDirectory() as td:
            class BadManifest:
                def write(self, s):
                    raise OSError("manifest boom")

                def flush(self):
                    return None

                def close(self):
                    return None

            w = make_writer(td)
            w._manifest_fh = BadManifest()
            w.submit("g/1", "2025-01-01", '{"a":1}')
            w.close()
            self.assertTrue((Path(td) / "g" / "1" / "2025-01-01.000.jsonl").exists())

    def test_warn_when_logger_raises(self):
        with tempfile.TemporaryDirectory() as td:
            class BadLogger:
                def info(self, *a, **k):
                    raise RuntimeError("boom")

                def warning(self, *a, **k):
                    raise RuntimeError("boom")

            class BadManifest:
                def write(self, s):
                    raise OSError("manifest boom")

                def flush(self):
                    return None

                def close(self):
                    return None

            w = make_writer(td, logger=BadLogger())
            w._manifest_fh = BadManifest()
            w.submit("g/1", "2025-01-01", '{"a":1}')
            w.close()  # 连日志都炸了也不能影响落盘

    def test_resume_stat_error_still_writes(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "g" / "1"
            d.mkdir(parents=True)
            f = d / "2025-01-01.000.jsonl"
            f.write_text('{"old":1}\n', encoding="utf-8")

            real_stat = Path.stat
            target = f.name

            def fake_stat(self, *a, **k):
                if self.name == target:
                    raise OSError("stat boom")
                return real_stat(self, *a, **k)

            with mock.patch.object(Path, "stat", fake_stat):
                w = make_writer(td)
                w.submit("g/1", "2025-01-01", '{"new":1}')
                w.close()
            self.assertGreaterEqual(f.stat().st_size, 10)


class WriterLockTests(unittest.TestCase):
    def test_retry_lock_states(self):
        with tempfile.TemporaryDirectory() as td:
            w1 = make_writer(td)
            w2 = make_writer(td)
            try:
                self.assertTrue(w1.retry_lock())   # 已经拿着锁
                self.assertFalse(w2.retry_lock())  # 还是抢不到
            finally:
                w2.close()
                self.assertFalse(w2.retry_lock())  # 关掉之后不再重试
                w1.close()

    def test_conflict_detection(self):
        self.assertTrue(writer._is_lock_conflict(OSError(errno.EACCES, "locked")))
        self.assertTrue(writer._is_lock_conflict(OSError(errno.EAGAIN, "busy")))
        self.assertFalse(writer._is_lock_conflict(OSError(errno.EOPNOTSUPP, "no locks")))
        win = OSError(0, "x")
        win.winerror = 33
        self.assertTrue(writer._is_lock_conflict(win))

    def test_lock_file_create_failure_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("builtins.open", side_effect=OSError("no lock file")):
                w = make_writer(td)
            self.assertTrue(w.locked)  # 建不了锁文件就不拦着写
            w.submit("g/1", "2025-01-01", '{"a":1}')
            w.close()
            self.assertEqual(B.read_jsonl(Path(td) / "g" / "1" / "2025-01-01.000.jsonl"),
                             [{"a": 1}])

    def test_unlock_error_still_closes(self):
        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            self.assertTrue(w.locked)
            with mock.patch("msvcrt.locking", side_effect=OSError("unlock boom")):
                w.close()  # 解锁失败也不能抛
            self.assertIsNone(w._lock_fh)

    def test_submit_when_locked_out(self):
        with tempfile.TemporaryDirectory() as td:
            w1 = make_writer(td)
            w2 = make_writer(td)
            try:
                self.assertTrue(w1.locked)
                self.assertFalse(w2.locked)
                self.assertIsNone(w2._thread)
                self.assertFalse(w2.submit("g/1", "2025-01-01", "{}"))
                self.assertEqual(w2.dropped, 1)
            finally:
                w2.close()
                w1.close()

    def test_unsupported_filesystem_proceeds(self):
        # 网络盘不支持字节锁时不能因此停采
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("msvcrt.locking",
                            side_effect=OSError(errno.EOPNOTSUPP, "no lock support")):
                w = make_writer(td)
            try:
                self.assertTrue(w.locked)
                w.submit("g/1", "2025-01-01", '{"a":1}')
            finally:
                w.close()
            self.assertEqual(B.read_jsonl(Path(td) / "g" / "1" / "2025-01-01.000.jsonl"),
                             [{"a": 1}])

    def test_close_failure_after_lock_denied(self):
        class BadClose:
            def __init__(self, fh):
                self._fh = fh

            def seek(self, *a):
                return self._fh.seek(*a)

            def fileno(self):
                return self._fh.fileno()

            def close(self):
                self._fh.close()  # 真实句柄要关掉，否则 Windows 删不掉临时目录
                raise OSError("close boom")

            def __getattr__(self, item):
                return getattr(self._fh, item)

        real_open = open
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("builtins.open",
                            side_effect=lambda *a, **k: BadClose(real_open(*a, **k))), \
                    mock.patch("msvcrt.locking",
                               side_effect=OSError(errno.EACCES, "locked")):
                w = make_writer(td)
            self.assertFalse(w.locked)
            w.close()

    def test_release_lock_close_error_swallowed(self):
        class BadClose:
            def __init__(self, fh):
                self._fh = fh

            def seek(self, *a):
                return self._fh.seek(*a)

            def fileno(self):
                return self._fh.fileno()

            def close(self):
                self._fh.close()
                raise OSError("close boom")

            def __getattr__(self, item):
                return getattr(self._fh, item)

        with tempfile.TemporaryDirectory() as td:
            w = make_writer(td)
            self.assertTrue(w.locked)
            w._lock_fh = BadClose(w._lock_fh)
            w.close()  # 解锁成功但关句柄失败，也不能抛
            self.assertIsNone(w._lock_fh)


class RecordDeepTests(unittest.TestCase):
    def test_s_helpers(self):
        self.assertEqual(record._s(True), "1")
        self.assertEqual(record._s(False), "0")
        self.assertEqual(record._s({"a": 1}), "")
        self.assertEqual(record._s([1]), "")

    def test_scalars_non_dict(self):
        self.assertEqual(record._scalars("nope"), {})
        self.assertEqual(record._scalars(None), {})

    def test_dump_without_dict(self):
        self.assertEqual(record._dump(42), {})

    def test_dump_skips_private_keys(self):
        out = record._dump(SimpleNamespace(_hidden=1, shown=2))
        self.assertEqual(out, {"shown": 2})

    def test_attr_swallows_property_error(self):
        class Evil:
            @property
            def x(self):
                raise RuntimeError("boom")

        self.assertEqual(record._attr(Evil(), "x", "dflt"), "dflt")

    def test_json_component(self):
        class Json:
            data = '{"app":"x"}'

        env = record.build_envelope(
            B.make_event(components=[Json()]), CollectConfig(), 1
        )
        seg = env.record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "json")
        self.assertEqual(seg["v"], '{"app":"x"}')

    def test_segment_exception_falls_back(self):
        class BadDict(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        env = record.build_envelope(
            B.make_event(components=[BadDict()]), CollectConfig(), 1
        )
        seg = env.record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "unknown")
        self.assertEqual(seg["cls"], "BadDict")

    def test_message_type_unknown(self):
        obj = SimpleNamespace(type=SimpleNamespace(name="SYSTEM_NOTICE"))
        self.assertEqual(record._message_type(obj, {}), "")

    def test_message_type_from_enum_name(self):
        obj = SimpleNamespace(
            type=SimpleNamespace(name="FRIEND_MESSAGE"),
            self_id="10001",
            message_id="m",
            group_id="",
            sender=SimpleNamespace(user_id="777"),
            message=[Comp.Plain("x")],
            message_str="x",
            raw_message={},  # 没有 message_type，只能靠枚举名
            timestamp=1756700000,
        )
        env = record.build_envelope(B.AstrMessageEvent(obj), CollectConfig(), 1)
        self.assertEqual(env.conv_rel, "p/777_10001")


class StatsTests(unittest.TestCase):
    def test_counter_basics(self):
        st = B.stats.Stats()
        st.inc("a")
        st.inc("a", 2)
        self.assertEqual(st.get("a"), 3)
        self.assertEqual(st.snapshot(), {"a": 3})
        st.reset()
        self.assertEqual(st.get("a"), 0)
        self.assertEqual(st.snapshot(), {})


class ConfigDeepTests(unittest.TestCase):
    def test_as_list_none(self):
        self.assertEqual(config._as_list(None), [])

    def test_resolve_root_import_failure(self):
        with mock.patch.dict(sys.modules, {"astrbot.core.utils.astrbot_path": None}):
            root = CollectConfig().resolve_root()
        self.assertTrue(str(root).replace("\\", "/").endswith("data/logs"))


class IngestorDeepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-deep-")
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
        ing = Ingestor(CollectConfig.from_mapping(base), B.stub_logger)
        await ing.start()
        self._made.append(ing)
        return ing

    async def test_aclose_swallows_task_exception(self):
        ing = await self.make()
        ing._task.cancel()
        await asyncio.wait([ing._task], timeout=2)

        async def boom():
            raise RuntimeError("task boom")

        ing._task = asyncio.create_task(boom())
        await asyncio.sleep(0.05)
        await ing.aclose()  # 不应抛

    async def test_dedup_cache_trimmed(self):
        ing = await self.make(dedup_cache_size=1000)
        for i in range(1005):
            ing._duplicate("s", "g/1", f"m{i}")
        self.assertLessEqual(len(ing._dedup), 1000)

    async def test_writer_submit_false_counted(self):
        ing = await self.make()
        ing.writer.submit = mock.Mock(return_value=False)
        ing.offer(B.make_event(text="x", msg_id="m1"))
        await asyncio.sleep(0.1)
        self.assertGreaterEqual(ing.stats.get("dropped_writer"), 1)

    async def test_cancel_during_media(self):
        ing = await self.make(download_media=True)

        async def slow(url):
            await asyncio.sleep(1)
            return {"err": "late"}

        ing.media.fetch = slow  # type: ignore[union-attr]
        ing.offer(B.make_event(components=[Comp.Image(url="http://x/y.png")], msg_id="m1"))
        await asyncio.sleep(0.05)
        ing._task.cancel()
        await asyncio.wait([ing._task], timeout=2)
        self.assertTrue(ing._task.cancelled())

    async def test_log_stats_survives_bad_logger(self):
        ing = await self.make()

        class BadLogger:
            def info(self, *a, **k):
                raise RuntimeError("boom")

            def warning(self, *a, **k):
                raise RuntimeError("boom")

        ing.logger = BadLogger()
        ing._log_stats()
        ing._warn("x", None)
        ing._warn("x", ValueError("y"))

    async def test_warn_without_logger(self):
        ing = await self.make()
        ing.logger = None
        ing._warn("x", None)
        ing._log_stats()


class MainDeepTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_message_reuses_ingestor(self):
        with tempfile.TemporaryDirectory() as td:
            p = B.main.TrainLogPlugin(
                context=None,
                config={"data_dir": td, "log_stats_interval_s": 0, "fsync_interval_s": 0},
            )
            await p.on_all_message(B.make_event(text="a", msg_id="m1"))
            first = p._ingestor
            await p.on_all_message(B.make_event(text="b", msg_id="m2"))
            self.assertIs(p._ingestor, first)  # 命中缓存分支
            await p.terminate()
            self.assertEqual(len(B.all_records(Path(td))), 2)


if __name__ == "__main__":
    unittest.main()