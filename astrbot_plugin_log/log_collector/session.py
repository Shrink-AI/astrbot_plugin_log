"""会话标识：把一条消息归约成「一个目录」。

会话只有两类落盘形态：
- 群聊   -> g/<群号>
- 私聊   -> p/<小号>_<大号>   （与原消息方向无关，A-B 和 B-A 落到同一目录）
另外群临时会话(t/)、频道(c/)单独隔离，避免和上面两类撞 id。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Tuple

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


def to_digits(v: Any) -> str:
    """只保留数字，用于拼路径，避免 'all'、空白、非法字符污染目录名。"""
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    return "".join(ch for ch in s if ch.isdigit())


def pair_key(a: Any, b: Any) -> str:
    """两个 QQ 号的无序拼法：按数值大小排序，保证 a-b 与 b-a 同键。"""
    a = to_digits(a) or "0"
    b = to_digits(b) or "0"
    if (len(a), a) <= (len(b), b):
        return f"{a}_{b}"
    return f"{b}_{a}"


def conv_of(
    message_type: str,
    group_id: Any,
    user_id: Any,
    self_id: Any,
    sub_type: str = "",
) -> Tuple[Dict[str, Any], str]:
    """返回 (conv 元信息, 相对目录路径)。"""
    gid = to_digits(group_id)
    sid = to_digits(self_id)
    uid = to_digits(user_id)
    mt = (message_type or "").lower()

    if mt == "group" and gid:
        return {"kind": "group", "id": gid, "key": f"g/{gid}"}, f"g/{gid}"

    if mt == "private":
        pair = pair_key(uid, sid)
        if (sub_type or "").lower() == "group" and gid:
            return (
                {"kind": "temp", "id": f"{gid}:{pair}", "key": f"t/{gid}/{pair}"},
                f"t/{gid}/{pair}",
            )
        return {"kind": "private", "id": pair, "key": f"p/{pair}"}, f"p/{pair}"

    if gid:
        return {"kind": mt or "group", "id": gid, "key": f"g/{gid}"}, f"g/{gid}"

    pair = pair_key(uid, sid)
    return {"kind": mt or "private", "id": pair, "key": f"p/{pair}"}, f"p/{pair}"


_TZ_CACHE: Dict[str, Any] = {}


def get_tz(name: str):
    if not name:
        return None
    if name in _TZ_CACHE:
        return _TZ_CACHE[name]
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(name)
        except Exception:
            tz = None
    _TZ_CACHE[name] = tz
    return tz


def day_of(ts_ms: int, tz_name: str) -> str:
    tz = get_tz(tz_name)
    try:
        if tz is not None:
            return datetime.fromtimestamp(ts_ms / 1000.0, tz).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        pass
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return datetime.now().strftime("%Y-%m-%d")


def timestamp_ms(message_obj: Any, raw: Dict[str, Any]) -> int:
    """平台时间戳，统一成毫秒。aiocqhttp 给的是秒。"""
    candidates = [
        getattr(message_obj, "timestamp", None),
        raw.get("time"),
        raw.get("timestamp"),
    ]
    for c in candidates:
        try:
            v = int(c)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        return v * 1000 if v < 10**11 else v
    return int(datetime.now().timestamp() * 1000)