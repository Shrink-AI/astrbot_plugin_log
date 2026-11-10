"""把事件拍平成记录：字段、组件保真、异常输入的容错。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import _bootstrap as B

record = B.record
CollectConfig = B.config.CollectConfig
Comp = B.Comp


def envelope(ev, cfg=None, seq=1):
    return record.build_envelope(ev, cfg or CollectConfig(), seq)


class MessageBasicsTests(unittest.TestCase):
    def test_group_message(self):
        ev = B.make_event(text="你好", user_id="111", group_id="123456789", msg_id="42")
        env = envelope(ev)
        self.assertIsNotNone(env)
        r = env.record
        self.assertEqual(r["v"], 1)
        self.assertEqual(r["type"], "message")
        self.assertEqual(r["msg_id"], "42")
        self.assertEqual(r["self_id"], "10001")
        self.assertEqual(r["conv"], {"kind": "group", "id": "123456789", "key": "g/123456789"})
        self.assertEqual(env.conv_rel, "g/123456789")
        self.assertEqual(r["sender"]["id"], "111")
        self.assertEqual(r["sender"]["card"], "card111")
        self.assertEqual(r["msg"]["text"], "你好")
        self.assertEqual(r["msg"]["chars"], 2)
        self.assertEqual(r["msg"]["kinds"], ["text"])
        self.assertFalse(r["sender"]["is_self"])
        self.assertIsInstance(r["recv_ts"], int)
        self.assertEqual(r["local_seq"], 1)

    def test_private_pair_order(self):
        ev = B.make_event(message_type="private", group_id="", user_id="222", text="在吗")
        env = envelope(ev)
        self.assertEqual(env.conv_rel, "p/222_10001")
        self.assertEqual(env.record["conv"]["kind"], "private")

    def test_temp_session(self):
        ev = B.make_event(
            message_type="private", group_id="555", user_id="222", sub_type="group", text="临时"
        )
        env = envelope(ev)
        self.assertEqual(env.conv_rel, "t/555/222_10001")

    def test_is_self_flag(self):
        ev = B.make_event(user_id="10001", text="我是 bot")
        env = envelope(ev)
        self.assertTrue(env.record["sender"]["is_self"])

    def test_empty_message(self):
        ev = B.make_event(text="")
        env = envelope(ev)
        r = env.record
        self.assertEqual(r["msg"]["text"], "")
        self.assertEqual(r["msg"]["chars"], 0)
        self.assertEqual(r["msg"]["kinds"], [])
        self.assertEqual(r["msg"]["segments"], [])

    def test_unicode_and_emoji(self):
        text = "😀🎉中文·combining é"
        env = envelope(B.make_event(text=text))
        self.assertEqual(env.record["msg"]["text"], text)
        self.assertEqual(env.record["msg"]["chars"], len(text))

    def test_very_long_text(self):
        text = "长" * 50000
        env = envelope(B.make_event(text=text))
        self.assertEqual(len(env.record["msg"]["text"]), 50000)


class SegmentTests(unittest.TestCase):
    def test_at_component(self):
        comps = [Comp.AtAll(), Comp.At(qq="222", name="B"), Comp.Plain("hi")]
        env = envelope(B.make_event(components=comps))
        self.assertEqual(env.record["msg"]["at"], ["all", "222"])

    def test_reply_component(self):
        comps = [Comp.Reply(id="m0"), Comp.Plain("回复")]
        env = envelope(B.make_event(components=comps))
        self.assertEqual(env.record["msg"]["reply_to"], "m0")

    def test_image_component(self):
        env = envelope(B.make_event(components=[Comp.Image(file="", url="http://x/y.png")]))
        seg = env.record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "image")
        self.assertEqual(seg["url"], "http://x/y.png")

    def test_unknown_component_kept(self):
        env = envelope(B.make_event(components=[Comp.Unknown(foo=1, bar="x")]))
        seg = env.record["msg"]["segments"][0]
        self.assertEqual(seg["t"], "unknown")
        self.assertEqual(seg["cls"], "Unknown")
        self.assertEqual(seg["dump"], {"foo": 1, "bar": "x"})

    def test_raw_dict_segments(self):
        comps = [
            {"type": "text", "data": {"text": "hi"}},
            {"type": "image", "data": {"file": "http://x/y.png"}},
            {"type": "at", "data": {"qq": "222"}},
            {"type": "weird", "data": {"k": 1}},
        ]
        env = envelope(B.make_event(components=comps))
        segs = env.record["msg"]["segments"]
        self.assertEqual(segs[0], {"t": "text", "v": "hi"})
        self.assertEqual(segs[1]["t"], "image")
        self.assertEqual(segs[1]["url"], "http://x/y.png")
        self.assertEqual(segs[2], {"t": "at", "qq": "222", "name": ""})
        self.assertEqual(segs[3]["t"], "unknown")
        self.assertEqual(segs[3]["cls"], "dict:weird")

    def test_text_fallback_without_message_str(self):
        obj = SimpleNamespace(
            type=SimpleNamespace(name="GROUP_MESSAGE"),
            self_id="10001",
            message_id="m9",
            group_id="123",
            sender=SimpleNamespace(user_id="111"),
            message=[Comp.Plain("a"), Comp.Plain("b")],
            message_str="",
            raw_message={"message_type": "group", "group_id": "123", "user_id": "111"},
            timestamp=1756700000,
        )
        env = envelope(B.AstrMessageEvent(obj))
        self.assertEqual(env.record["msg"]["text"], "ab")

    def test_kinds_deduplicated_in_order(self):
        comps = [Comp.Plain("a"), Comp.Image(url="u"), Comp.Plain("b")]
        env = envelope(B.make_event(components=comps))
        self.assertEqual(env.record["msg"]["kinds"], ["text", "image"])


class RawHandlingTests(unittest.TestCase):
    def test_raw_as_object(self):
        env = envelope(B.make_event(text="x", raw_as_object=True))
        self.assertEqual(env.record["sender"]["card"], "card111")
        self.assertEqual(env.record["sender"]["role"], "member")
        self.assertNotIn("raw", env.record)

    def test_store_raw_event(self):
        cfg = CollectConfig.from_mapping({"store_raw_event": True})
        env = envelope(B.make_event(text="x"), cfg)
        self.assertIn("raw", env.record)
        self.assertEqual(env.record["raw"]["message_id"], "m1")

    def test_missing_raw_message_still_works(self):
        env = envelope(B.make_event(text="x", drop_raw=True))
        self.assertEqual(env.record["msg"]["text"], "x")
        self.assertEqual(env.record["conv"]["key"], "g/123456789")

    def test_missing_sender_attribute(self):
        obj = SimpleNamespace(
            type=SimpleNamespace(name="GROUP_MESSAGE"),
            self_id="10001",
            message_id="m2",
            group_id="123",
            message=[Comp.Plain("x")],
            message_str="x",
            raw_message={"message_type": "group", "group_id": "123"},
            timestamp=1756700000,
        )
        env = envelope(B.AstrMessageEvent(obj))
        self.assertEqual(env.record["sender"]["id"], "")
        self.assertEqual(env.record["conv"]["id"], "123")

    def test_no_message_obj(self):
        self.assertIsNone(record.build_envelope(SimpleNamespace(), CollectConfig(), 1))


class MessageSentTests(unittest.TestCase):
    def test_message_sent_treated_as_message(self):
        ev = B.make_event(
            user_id="10001", text="bot 自己说的", raw_extra={"post_type": "message_sent"}
        )
        env = envelope(ev)
        self.assertEqual(env.record["type"], "message")
        self.assertTrue(env.record["sender"]["is_self"])


class NoticeTests(unittest.TestCase):
    def test_group_recall(self):
        ev = B.make_event(notice="group_recall", group_id="123456789", user_id="777")
        env = envelope(ev)
        r = env.record
        self.assertEqual(r["type"], "notice")
        self.assertEqual(r["notice"], "group_recall")
        self.assertEqual(r["conv"]["key"], "g/123456789")
        self.assertEqual(r["actor"]["id"], "777")
        self.assertEqual(env.conv_rel, "g/123456789")

    def test_group_ban_scalars(self):
        ev = B.make_event(
            notice="group_ban", group_id="123456789", user_id="888",
            raw_extra={"duration": 600, "target_id": "999"},
        )
        env = envelope(ev)
        self.assertEqual(env.record["detail"]["duration"], 600)
        self.assertEqual(env.record["target"]["id"], "999")

    def test_notice_without_group_or_actor(self):
        ev = B.make_event(notice="friend_recall", group_id="", user_id="")
        self.assertIsNone(envelope(ev))

    def test_notice_private(self):
        ev = B.make_event(notice="friend_recall", message_type="private", group_id="", user_id="222")
        env = envelope(ev)
        self.assertEqual(env.conv_rel, "p/222_10001")


class TimestampInRecordTests(unittest.TestCase):
    def test_ts_and_day_consistent(self):
        ts = 1756700000000
        env = envelope(B.make_event(ts=ts))
        self.assertEqual(env.record["ts"], ts)
        self.assertEqual(env.record["day"], B.session.day_of(ts, "Asia/Shanghai"))
        self.assertEqual(env.day, env.record["day"])


if __name__ == "__main__":
    unittest.main()