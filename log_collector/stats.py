"""线程安全的计数器，供写入线程与事件循环共同累加。"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict


class Stats:
    def __init__(self) -> None:
        self._d: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._d[key] += n

    def get(self, key: str) -> int:
        with self._lock:
            return self._d.get(key, 0)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._d)

    def reset(self) -> None:
        with self._lock:
            self._d.clear()