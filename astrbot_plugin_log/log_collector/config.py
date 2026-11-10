"""插件配置：把 AstrBot 注入的 dict 归一化成强类型 dataclass。"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, List

PLUGIN_NAME = "astrbot_plugin_log"


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y")
    return default


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [p.strip() for p in v.replace("\n", ",").split(",") if p.strip()]
    return []


@dataclass
class CollectConfig:
    enabled: bool = True
    data_dir: str = ""
    max_file_mb: int = 128
    collect_group: bool = True
    collect_private: bool = True
    collect_temp_session: bool = True
    group_whitelist: List[str] = None  # type: ignore[assignment]
    group_blacklist: List[str] = None  # type: ignore[assignment]
    ignore_self: bool = False
    store_raw_event: bool = False
    timezone: str = "Asia/Shanghai"
    download_media: bool = True
    media_kinds: List[str] = None  # type: ignore[assignment]
    media_max_mb: int = 20
    media_timeout_s: int = 10
    media_concurrency: int = 4
    queue_size: int = 5000
    dedup_cache_size: int = 200000
    fsync_interval_s: int = 5
    log_stats_interval_s: int = 300

    def __post_init__(self) -> None:
        if self.group_whitelist is None:
            self.group_whitelist = []
        if self.group_blacklist is None:
            self.group_blacklist = []
        if self.media_kinds is None:
            self.media_kinds = ["image"]

    @classmethod
    def from_mapping(cls, m: Any) -> "CollectConfig":
        cfg = cls()
        if not hasattr(m, "get"):
            return cfg
        for f in fields(cls):
            try:
                raw = m.get(f.name, None)
            except Exception:
                raw = None
            if raw is None:
                continue
            default = getattr(cfg, f.name)
            if isinstance(default, bool):
                setattr(cfg, f.name, _as_bool(raw, default))
            elif isinstance(default, int):
                setattr(cfg, f.name, _as_int(raw, default))
            elif isinstance(default, (list, tuple)) or default is None:
                setattr(cfg, f.name, _as_list(raw))
            else:
                setattr(cfg, f.name, str(raw))
        cfg.__post_init__()
        return cfg

    @property
    def max_file_bytes(self) -> int:
        return max(1, self.max_file_mb) * 1024 * 1024

    @property
    def media_max_bytes(self) -> int:
        return max(1, self.media_max_mb) * 1024 * 1024

    def resolve_root(self) -> Path:
        """数据根目录：显式配置优先，否则遵循 AstrBot 的 plugin_data 约定。"""
        if self.data_dir and self.data_dir.strip():
            return Path(self.data_dir.strip()).expanduser().resolve()
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return (
                Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "logs"
            ).resolve()
        except Exception:
            return (Path(__file__).resolve().parent.parent / "data" / "logs").resolve()

    def group_allowed(self, group_id: str) -> bool:
        gid = str(group_id)
        if self.group_whitelist and gid not in self.group_whitelist:
            return False
        if self.group_blacklist and gid in self.group_blacklist:
            return False
        return True