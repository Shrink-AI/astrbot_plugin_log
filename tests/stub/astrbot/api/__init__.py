from __future__ import annotations


class _Logger:
    def __init__(self) -> None:
        self.records = []

    def _log(self, level, msg, *args, **kwargs):
        self.records.append((level, str(msg)))

    def debug(self, msg, *a, **k):
        self._log("debug", msg)

    def info(self, msg, *a, **k):
        self._log("info", msg)

    def warning(self, msg, *a, **k):
        self._log("warning", msg)

    def error(self, msg, *a, **k):
        self._log("error", msg)

    def exception(self, msg, *a, **k):
        self._log("error", msg)


logger = _Logger()


class AstrBotConfig(dict):
    pass