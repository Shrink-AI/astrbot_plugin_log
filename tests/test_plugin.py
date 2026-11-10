"""插件外壳：开关、回调容错、失败闩锁、生命周期。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _bootstrap as B

main = B.main


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="trainlog-plug-")
        self.root = Path(self._td.name)

    async def asyncTearDown(self):
        self._td.cleanup()

    def plugin(self, **cfg):
        base = {
            "data_dir": str(self.root),
            "log_stats_interval_s": 0,
            "fsync_interval_s": 0,
        }
        base.update(cfg)
        return main.TrainLogPlugin(context=None, config=base)

    def test_handler_registered(self):
        self.assertEqual(getattr(main.TrainLogPlugin.on_all_message, "__event_type__", None), "all")

    async def test_defaults_without_config(self):
        p = main.TrainLogPlugin(context=None, config=None)
        self.assertEqual(p.cfg.max_file_mb, 128)
        self.assertTrue(p.cfg.enabled)
        await p.terminate()  # 从未启动过也不能炸

    async def test_disabled_does_nothing(self):
        p = self.plugin(enabled=False)
        await p.on_all_message(B.make_event(text="x"))
        self.assertIsNone(p._ingestor)
        await p.terminate()
        self.assertEqual(B.all_records(self.root), [])

    async def test_collects_message(self):
        p = self.plugin()
        await p.on_all_message(B.make_event(text="hi", msg_id="m1"))
        await p.terminate()
        rows = B.all_records(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["msg"]["text"], "hi")

    async def test_handler_swallows_exceptions(self):
        p = self.plugin()

        async def boom():
            raise RuntimeError("boom")

        p._get_ingestor = boom
        await p.on_all_message(B.make_event(text="x"))  # 不应抛异常
        self.assertTrue(p._failed)

    async def test_init_failure_logged_once(self):
        p = self.plugin()
        original = main.Ingestor

        def boom(*a, **k):
            raise OSError("no access")

        main.Ingestor = boom
        try:
            before = sum(1 for r in B.stub_logger.records if "初始化失败" in r[1])
            await p.on_all_message(B.make_event(text="x"))
            await p.on_all_message(B.make_event(text="y"))
            await p.on_all_message(B.make_event(text="z"))
            after = sum(1 for r in B.stub_logger.records if "初始化失败" in r[1])
        finally:
            main.Ingestor = original
        self.assertEqual(after - before, 1)
        self.assertTrue(p._failed)

    async def test_terminate_twice(self):
        p = self.plugin()
        await p.on_all_message(B.make_event(text="x", msg_id="m1"))
        await p.terminate()
        await p.terminate()
        self.assertEqual(len(B.all_records(self.root)), 1)
        self.assertIsNone(p._ingestor)

    async def test_reload_creates_new_ingestor(self):
        p = self.plugin()
        await p.on_all_message(B.make_event(text="x", msg_id="m1"))
        first = p._ingestor
        await p.terminate()
        self.assertIsNone(p._ingestor)
        await p.on_all_message(B.make_event(text="y", msg_id="m2"))  # 停用后再来消息会重启管线
        self.assertIsNotNone(p._ingestor)
        self.assertIsNot(p._ingestor, first)
        await p.terminate()
        self.assertEqual(len(B.all_records(self.root)), 2)


if __name__ == "__main__":
    unittest.main()