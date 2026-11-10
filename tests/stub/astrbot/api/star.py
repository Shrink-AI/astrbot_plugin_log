from __future__ import annotations


class Context:
    def __init__(self):
        self._config = {}

    def get_config(self, *a, **k):
        return self._config


class Star:
    def __init__(self, context=None):
        self.context = context
        self.name = "astrbot_plugin_log"