"""会话标识与时间的单元测试。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import _bootstrap as B

session = B.session

_SH = session.get_tz("Asia/Shanghai")
TZ_READY = _SH is not None


class ToDigitsTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(session.to_digits("123456"), "123456")

    def test_int(self):
        self.assertEqual(session.to_digits(123456), "123456")

    def test_mixed(self):
        self.assertEqual(session.to_digits(" u_123abc "), "123")

    def test_empty(self):
        self.assertEqual(session.to_digits(None), "")
        self.assertEqual(session.to_digits(""), "")
        self.assertEqual(session.to_digits("abc"), "")


class PairKeyTests(unittest.TestCase):
    def test_order_independent(self):
        self.assertEqual(session.pair_key("111", "222"), session.pair_key("222", "111"))

    def test_self_pair(self):
        self.assertEqual(session.pair_key("10001", "10001"), "10001_10001")

    def test_numeric_not_lexical(self):
        # 纯字符串比较会得到 1000_999，是错误的
        self.assertEqual(session.pair_key("1000", "999"), "999_1000")

    def test_three_vs_five_digits(self):
        self.assertEqual(session.pair_key("10001", "777"), "777_10001")

    def test_non_digit_input(self):
        self.assertEqual(session.pair_key("u111", "222"), "111_222")

    def test_missing_becomes_zero(self):
        self.assertEqual(session.pair_key("", ""), "0_0")
        self.assertEqual(session.pair_key(None, "222"), "0_222")


class ConvOfTests(unittest.TestCase):
    def test_group(self):
        conv, rel = session.conv_of("group", "123456789", "111", "10001")
        self.assertEqual(rel, "g/123456789")
        self.assertEqual(conv, {"kind": "group", "id": "123456789", "key": "g/123456789"})

    def test_private(self):
        conv, rel = session.conv_of("private", "", "222", "10001")
        self.assertEqual(rel, "p/222_10001")
        self.assertEqual(conv["kind"], "private")

    def test_private_order_insensitive(self):
        a, ra = session.conv_of("private", "", "222", "10001")
        b, rb = session.conv_of("private", "", "10001", "222")
        self.assertEqual(ra, rb)
        self.assertEqual(a["id"], b["id"])

    def test_temp_session_isolated(self):
        _, rel = session.conv_of("private", "123456789", "222", "10001", sub_type="group")
        self.assertEqual(rel, "t/123456789/222_10001")

    def test_unknown_type_with_group(self):
        conv, rel = session.conv_of("", "555", "111", "10001")
        self.assertEqual(rel, "g/555")

    def test_unknown_type_without_group(self):
        _, rel = session.conv_of("", "", "111", "10001")
        self.assertEqual(rel, "p/111_10001")

    def test_all_empty(self):
        _, rel = session.conv_of("", "", "", "")
        self.assertEqual(rel, "p/0_0")


class DayOfTests(unittest.TestCase):
    @unittest.skipUnless(TZ_READY, "本机没有 tzdata，跳过时区用例")
    def test_matches_datetime(self):
        ts = 1756700000000
        expect = datetime.fromtimestamp(ts / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        self.assertEqual(session.day_of(ts, "Asia/Shanghai"), expect)

    @unittest.skipUnless(TZ_READY, "本机没有 tzdata，跳过时区用例")
    def test_midnight_boundary(self):
        tz = _SH
        before = datetime(2025, 9, 1, 23, 59, 59, tzinfo=tz)
        after = datetime(2025, 9, 2, 0, 0, 0, tzinfo=tz)
        a = session.day_of(int(before.timestamp() * 1000), "Asia/Shanghai")
        b = session.day_of(int(after.timestamp() * 1000), "Asia/Shanghai")
        self.assertEqual(a, "2025-09-01")
        self.assertEqual(b, "2025-09-02")

    @unittest.skipUnless(TZ_READY, "本机没有 tzdata，跳过时区用例")
    def test_utc_vs_shanghai_differs(self):
        # 2025-09-01 00:30 +08:00 == 2025-08-31 16:30 UTC
        ts = int(datetime(2025, 9, 1, 0, 30, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        self.assertEqual(session.day_of(ts, "Asia/Shanghai"), "2025-09-01")
        self.assertEqual(session.day_of(ts, "UTC"), "2025-08-31")

    def test_invalid_timezone_falls_back(self):
        out = session.day_of(1756700000000, "Not/AZone")
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2}$")

    def test_empty_timezone(self):
        out = session.day_of(1756700000000, "")
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2}$")

    def test_out_of_range_does_not_raise(self):
        self.assertRegex(session.day_of(10**16, "Asia/Shanghai"), r"^\d{4}-\d{2}-\d{2}$")


class TimestampTests(unittest.TestCase):
    def test_seconds_to_ms(self):
        obj = B.SimpleNamespace(timestamp=1756700000)
        self.assertEqual(session.timestamp_ms(obj, {}), 1756700000000)

    def test_ms_passthrough(self):
        obj = B.SimpleNamespace(timestamp=1756700000123)
        self.assertEqual(session.timestamp_ms(obj, {}), 1756700000123)

    def test_from_raw_time(self):
        obj = B.SimpleNamespace(timestamp=None)
        self.assertEqual(session.timestamp_ms(obj, {"time": 1756700000}), 1756700000000)

    def test_zero_falls_through(self):
        obj = B.SimpleNamespace(timestamp=0)
        got = session.timestamp_ms(obj, {"time": 1756700000})
        self.assertEqual(got, 1756700000000)

    def test_all_missing_uses_now(self):
        obj = B.SimpleNamespace(timestamp=None)
        got = session.timestamp_ms(obj, {})
        self.assertGreater(got, 1_600_000_000_000)

    def test_garbage_values_ignored(self):
        obj = B.SimpleNamespace(timestamp="abc")
        got = session.timestamp_ms(obj, {"time": "def"})
        self.assertGreater(got, 1_600_000_000_000)


if __name__ == "__main__":
    unittest.main()