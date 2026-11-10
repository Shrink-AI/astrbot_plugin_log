"""配置解析的单元测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

import _bootstrap as B

cfgmod = B.config
CollectConfig = cfgmod.CollectConfig


class DefaultsTests(unittest.TestCase):
    def test_defaults(self):
        c = CollectConfig()
        self.assertTrue(c.enabled)
        self.assertEqual(c.max_file_mb, 128)
        self.assertTrue(c.download_media)
        self.assertEqual(c.media_kinds, ["image"])
        self.assertEqual(c.timezone, "Asia/Shanghai")
        self.assertEqual(c.max_file_bytes, 128 * 1024 * 1024)
        self.assertEqual(c.media_max_bytes, 20 * 1024 * 1024)

    def test_none_mapping(self):
        c = CollectConfig.from_mapping(None)
        self.assertEqual(c.max_file_mb, 128)

    def test_mapping_like_object_without_get(self):
        c = CollectConfig.from_mapping(object())
        self.assertEqual(c.max_file_mb, 128)


class CoercionTests(unittest.TestCase):
    def test_bool_strings(self):
        for raw, expect in (("true", True), ("false", False), ("1", True), ("0", False),
                            ("on", True), ("off", False), (1, True), (0, False)):
            c = CollectConfig.from_mapping({"enabled": raw})
            self.assertEqual(c.enabled, expect, f"enabled={raw!r}")

    def test_int_from_string(self):
        c = CollectConfig.from_mapping({"max_file_mb": "64"})
        self.assertEqual(c.max_file_mb, 64)
        self.assertEqual(c.max_file_bytes, 64 * 1024 * 1024)

    def test_int_garbage_falls_back(self):
        c = CollectConfig.from_mapping({"max_file_mb": "abc"})
        self.assertEqual(c.max_file_mb, 128)

    def test_list_from_comma_string(self):
        c = CollectConfig.from_mapping({"group_whitelist": "123, 456 ,789"})
        self.assertEqual(c.group_whitelist, ["123", "456", "789"])

    def test_list_from_newline_string(self):
        c = CollectConfig.from_mapping({"group_blacklist": "123\n456"})
        self.assertEqual(c.group_blacklist, ["123", "456"])

    def test_list_from_list(self):
        c = CollectConfig.from_mapping({"media_kinds": ["image", "record"]})
        self.assertEqual(c.media_kinds, ["image", "record"])

    def test_list_blank_items_dropped(self):
        c = CollectConfig.from_mapping({"group_whitelist": " , 123, "})
        self.assertEqual(c.group_whitelist, ["123"])

    def test_none_keeps_default(self):
        c = CollectConfig.from_mapping({"max_file_mb": None, "timezone": None})
        self.assertEqual(c.max_file_mb, 128)
        self.assertEqual(c.timezone, "Asia/Shanghai")

    def test_unknown_keys_ignored(self):
        c = CollectConfig.from_mapping({"nope": 1, "also_nope": "x"})
        self.assertFalse(hasattr(c, "nope"))

    def test_string_field(self):
        c = CollectConfig.from_mapping({"timezone": "UTC"})
        self.assertEqual(c.timezone, "UTC")


class ClampTests(unittest.TestCase):
    def test_zero_file_size_clamped(self):
        c = CollectConfig.from_mapping({"max_file_mb": 0})
        self.assertGreaterEqual(c.max_file_bytes, 1)
        c2 = CollectConfig.from_mapping({"max_file_mb": -5})
        self.assertGreaterEqual(c2.max_file_bytes, 1)

    def test_media_size_clamped(self):
        c = CollectConfig.from_mapping({"media_max_mb": 0})
        self.assertGreaterEqual(c.media_max_bytes, 1)


class RootTests(unittest.TestCase):
    def test_explicit_dir(self):
        c = CollectConfig.from_mapping({"data_dir": "D:/tmp/somewhere"})
        self.assertEqual(c.resolve_root(), Path("D:/tmp/somewhere").resolve())

    def test_blank_uses_plugin_data(self):
        c = CollectConfig.from_mapping({"data_dir": ""})
        root = c.resolve_root()
        self.assertTrue(str(root).endswith("logs"))
        self.assertIsInstance(root, Path)

    def test_relative_dir_is_absolute(self):
        c = CollectConfig.from_mapping({"data_dir": "rel/dir"})
        self.assertTrue(c.resolve_root().is_absolute())


class GroupAllowTests(unittest.TestCase):
    def test_no_filter(self):
        c = CollectConfig()
        self.assertTrue(c.group_allowed("123"))
        self.assertTrue(c.group_allowed("456"))

    def test_whitelist(self):
        c = CollectConfig.from_mapping({"group_whitelist": ["123"]})
        self.assertTrue(c.group_allowed("123"))
        self.assertFalse(c.group_allowed("456"))

    def test_blacklist(self):
        c = CollectConfig.from_mapping({"group_blacklist": ["456"]})
        self.assertTrue(c.group_allowed("123"))
        self.assertFalse(c.group_allowed("456"))

    def test_blacklist_wins(self):
        c = CollectConfig.from_mapping({"group_whitelist": ["123"], "group_blacklist": ["123"]})
        self.assertFalse(c.group_allowed("123"))


if __name__ == "__main__":
    unittest.main()