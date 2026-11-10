"""把 AstrBot 事件对象拍平成可训练的结构化记录。

设计原则：
- 原始信息尽量无损（富媒体组件、未知组件都保留），同时给一份派生字段（text/kinds）方便过滤
- 不依赖具体组件类的 import，只按类名分派，避免跟随 AstrBot 内部结构变化而崩
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .session import conv_of, day_of, pair_key, timestamp_ms, to_digits

SCHEMA_VERSION = 1


@dataclass
class Envelope:
    record: Dict[str, Any]
    conv_rel: str
    day: str


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (dict, list)):
        return ""
    return str(v)


def _scalars(d: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
    return out


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _as_dict(v: Any) -> Dict[str, Any]:
    """适配器给的原始事件可能是 dict，也可能是对象，统一成 dict。"""
    if isinstance(v, dict):
        return v
    for conv in (lambda x: dict(x), lambda x: vars(x)):
        try:
            got = conv(v)
        except Exception:
            continue
        if isinstance(got, dict):
            return {str(k): val for k, val in got.items() if not str(k).startswith("_")}
    return {}


def _dump(obj: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        items = vars(obj)
    except TypeError:
        return out
    for k, v in items.items():
        if k.startswith("_"):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
    return out


def _seg(c: Any) -> Dict[str, Any]:
    """单个消息组件 -> 紧凑 dict。"""
    name = type(c).__name__.lower()

    if name in ("plain", "plaintext"):
        return {"t": "text", "v": _s(_attr(c, "text"))}

    if name == "image":
        return {
            "t": "image",
            "url": _s(_attr(c, "url")),
            "file": _s(_attr(c, "file")),
            "sub": _s(_attr(c, "sub_type")),
        }

    if name == "at":
        return {"t": "at", "qq": _s(_attr(c, "qq")), "name": _s(_attr(c, "name"))}

    if name == "atall":
        return {"t": "at", "qq": "all", "name": ""}

    if name == "reply":
        return {"t": "reply", "id": _s(_attr(c, "id"))}

    if name == "face":
        return {"t": "face", "id": _s(_attr(c, "id"))}

    if name == "record":
        return {
            "t": "record",
            "url": _s(_attr(c, "url")),
            "file": _s(_attr(c, "file")),
            "text": _s(_attr(c, "text")),
        }

    if name == "video":
        return {"t": "video", "url": _s(_attr(c, "url")), "file": _s(_attr(c, "file"))}

    if name == "file":
        return {
            "t": "file",
            "name": _s(_attr(c, "name")),
            "url": _s(_attr(c, "url")),
            "file": _s(_attr(c, "file")),
        }

    if name in ("forward", "node", "nodes"):
        return {
            "t": "forward",
            "id": _s(_attr(c, "id")),
            "cls": type(c).__name__,
            "dump": _dump(c),
        }

    if name in ("json", "jsoncomponent", "xml", "xmlcomponent"):
        return {"t": name, "v": _s(_attr(c, "data") or _attr(c, "text"))}

    if name in ("poke", "shake", "dice", "rps"):
        return {"t": name, "dump": _dump(c)}

    # 未知组件：保留类名与标量字段，宁可多存也不要丢
    return {"t": "unknown", "cls": type(c).__name__, "dump": _dump(c)}


_DICT_URL_KEYS = ("url", "file")


def _seg_from_dict(c: Dict[str, Any]) -> Dict[str, Any]:
    """兼容「没被适配器归一化成组件」的原始 CQ 段。"""
    t = _s(c.get("type") or c.get("t")).lower()
    data = c.get("data") if isinstance(c.get("data"), dict) else c
    url = ""
    for k in _DICT_URL_KEYS:
        v = _s(data.get(k))
        if v.startswith(("http://", "https://")):
            url = v
            break
    if t in ("text", "plain"):
        return {"t": "text", "v": _s(data.get("text"))}
    if t == "at":
        return {"t": "at", "qq": _s(data.get("qq")), "name": _s(data.get("name"))}
    if t == "image":
        return {"t": "image", "url": url, "file": _s(data.get("file")), "sub": _s(data.get("sub_type"))}
    if t == "record":
        return {"t": "record", "url": url, "file": _s(data.get("file"))}
    if t == "video":
        return {"t": "video", "url": url, "file": _s(data.get("file"))}
    if t == "file":
        return {"t": "file", "name": _s(data.get("name")), "url": url, "file": _s(data.get("file"))}
    if t == "reply":
        return {"t": "reply", "id": _s(data.get("id"))}
    if t == "face":
        return {"t": "face", "id": _s(data.get("id"))}
    return {"t": "unknown", "cls": f"dict:{t}", "dump": _scalars(data)}


def _segments(chain: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        items = list(chain or ())
    except TypeError:
        return out
    for c in items:
        try:
            out.append(_seg_from_dict(c) if isinstance(c, dict) else _seg(c))
        except Exception:
            out.append({"t": "unknown", "cls": type(c).__name__})
    return out


def _message_type(message_obj: Any, raw: Dict[str, Any]) -> str:
    mt = _s(raw.get("message_type"))
    if mt:
        return mt
    t = _attr(message_obj, "type", None)
    name = _s(_attr(t, "name", "")) or _s(t)
    name = name.lower()
    if "group" in name:
        return "group"
    if "private" in name or "friend" in name:
        return "private"
    return ""


def build_envelope(event: Any, cfg: Any, seq: int, now_ms: Optional[int] = None) -> Optional[Envelope]:
    """事件 -> Envelope；无法识别时返回 None（绝不抛异常给调用方之外）。"""
    now_ms = now_ms or int(time.time() * 1000)

    message_obj = _attr(event, "message_obj", None)
    if message_obj is None:
        return None

    raw = _as_dict(_attr(message_obj, "raw_message", None))

    post_type = _s(raw.get("post_type"))
    self_id = _s(_attr(message_obj, "self_id", "")) or _s(raw.get("self_id"))

    # message_sent 是 bot 自己发出的消息（部分适配器会上报），按消息处理并标记 is_self
    if post_type and post_type not in ("message", "message_sent"):
        return _build_notice(raw, post_type, self_id, cfg, now_ms)

    group_id = _s(_attr(message_obj, "group_id", "")) or _s(raw.get("group_id"))
    sender_obj = _attr(message_obj, "sender", None)
    user_id = _s(_attr(sender_obj, "user_id", "")) or _s(raw.get("user_id"))

    message_type = _message_type(message_obj, raw)
    sub_type = _s(raw.get("sub_type"))

    conv, conv_rel = conv_of(message_type, group_id, user_id, self_id, sub_type)

    ts = timestamp_ms(message_obj, raw)
    day = day_of(ts, getattr(cfg, "timezone", "") or "")

    segments = _segments(_attr(message_obj, "message", None))
    text = _s(_attr(message_obj, "message_str", ""))
    if not text:
        text = "".join(s.get("v", "") for s in segments if s.get("t") == "text")

    kinds: List[str] = []
    ats: List[str] = []
    reply_to = ""
    for s in segments:
        t = s.get("t", "")
        if t and t not in kinds:
            kinds.append(t)
        if t == "at":
            ats.append(s.get("qq", ""))
        elif t == "reply" and not reply_to:
            reply_to = s.get("id", "")

    raw_sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}

    record: Dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "type": "message",
        "day": day,
        "ts": ts,
        "recv_ts": now_ms,
        "self_id": self_id,
        "msg_id": _s(_attr(message_obj, "message_id", "")) or _s(raw.get("message_id")),
        "seq": raw.get("message_seq", raw.get("real_seq")),
        "local_seq": seq,
        "conv": conv,
        "sender": {
            "id": to_digits(user_id),
            "nickname": _s(_attr(sender_obj, "nickname", "")) or _s(raw_sender.get("nickname")),
            "card": _s(raw_sender.get("card")) or _s(_attr(sender_obj, "card", "")),
            "role": _s(raw_sender.get("role")) or _s(_attr(sender_obj, "role", "")),
            "is_self": bool(self_id) and to_digits(user_id) == to_digits(self_id),
        },
        "msg": {
            "reply_to": reply_to,
            "at": ats,
            "kinds": kinds,
            "chars": len(text),
            "text": text,
            "segments": segments,
        },
    }

    if getattr(cfg, "store_raw_event", False):
        record["raw"] = raw

    return Envelope(record=record, conv_rel=conv_rel, day=day)


def _build_notice(
    raw: Dict[str, Any], post_type: str, self_id: str, cfg: Any, now_ms: int
) -> Optional[Envelope]:
    """撤回 / 入退群 / 禁言等事件，与会话消息落在同一个文件里，保证上下文不断链。"""
    group_id = _s(raw.get("group_id"))
    actor = _s(raw.get("user_id")) or _s(raw.get("operator_id"))
    target = _s(raw.get("target_id"))

    if group_id:
        conv, conv_rel = conv_of("group", group_id, actor, self_id)
    elif actor:
        pair = pair_key(actor, self_id)
        conv, conv_rel = {"kind": "private", "id": pair, "key": f"p/{pair}"}, f"p/{pair}"
    else:
        return None

    ts = timestamp_ms(None, raw)
    day = day_of(ts, getattr(cfg, "timezone", "") or "")

    notice = _s(raw.get("notice_type")) or _s(raw.get("sub_type")) or post_type

    record: Dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "type": "notice",
        "notice": notice,
        "day": day,
        "ts": ts,
        "recv_ts": now_ms,
        "self_id": self_id,
        "conv": conv,
        "actor": {"id": to_digits(actor)},
        "target": {
            "id": to_digits(target),
            "msg_id": _s(raw.get("message_id")),
        },
        "detail": _scalars(raw),
    }
    if getattr(cfg, "store_raw_event", False):
        record["raw"] = raw

    return Envelope(record=record, conv_rel=conv_rel, day=day)