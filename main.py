"""train_log —— 群聊 / 私聊训练语料采集插件。

落盘形态（一个文件 = 一个会话在一天内的消息，超过 128MB 自动滚动）：

    data/
      g/<群号>/<YYYY-MM-DD>.000.jsonl          群聊
      p/<小号>_<大号>/<YYYY-MM-DD>.000.jsonl    私聊（A-B 与 B-A 同目录）
      t/<群号>/<小号>_<大号>/...                群临时会话
      media/<sha256[:2]>/<sha256>.<ext>         图片等媒体，内容寻址
      _manifest.jsonl                          文件清单

每行一条 JSON，字段见 README.md。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from .log_collector.config import CollectConfig
    from .log_collector.ingestor import Ingestor
except ImportError:  # 兜底：以非包方式加载时
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from log_collector.config import CollectConfig
    from log_collector.ingestor import Ingestor


class TrainLogPlugin(Star):
    def __init__(self, context: Context, config=None):
        try:
            super().__init__(context)
        except TypeError:
            super().__init__()
        self.config = config or {}
        self.cfg = CollectConfig.from_mapping(self.config)
        self._ingestor = None
        self._lock = None
        self._failed = False

        if self.cfg.enabled:
            logger.info(
                f"[train_log] 已加载，数据目录: {self.cfg.resolve_root()}，"
                f"单文件上限 {self.cfg.max_file_mb}MB，媒体落盘: {self.cfg.download_media}"
            )
        else:
            logger.info("[train_log] enabled=false，不采集")

    async def _get_ingestor(self) -> Ingestor:
        if self._ingestor is not None:
            return self._ingestor
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._ingestor is None:
                ing = Ingestor(self.cfg, logger)
                await ing.start()
                self._ingestor = ing
        return self._ingestor

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """旁路采集：不回复、不中断，任何异常都不外抛。"""
        if not self.cfg.enabled or self._failed:
            return
        try:
            ing = await self._get_ingestor()
            ing.offer(event)
        except Exception as e:
            # 初始化失败通常是持久问题（权限/路径），停掉并只报一次，避免刷屏
            self._failed = True
            logger.error(f"[train_log] 采集初始化失败，本次运行已停用: {type(e).__name__}: {e}")

    async def terminate(self):
        ing, self._ingestor = self._ingestor, None
        if ing is not None:
            try:
                await ing.aclose()
                logger.info("[train_log] 已停止，缓冲已落盘")
            except Exception as e:
                logger.warning(f"[train_log] 停止时异常: {type(e).__name__}: {e}")