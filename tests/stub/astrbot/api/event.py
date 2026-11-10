from __future__ import annotations


class EventMessageType:
    ALL = "all"
    GROUP_MESSAGE = "group"
    PRIVATE_MESSAGE = "private"


class _Filter:
    EventMessageType = EventMessageType

    def event_message_type(self, t):
        def deco(fn):
            fn.__event_type__ = t
            return fn

        return deco

    def command(self, *a, **k):
        def deco(fn):
            return fn

        return deco


filter = _Filter()


class MessageEventResult:
    pass


class AstrMessageEvent:
    def __init__(self, message_obj):
        self.message_obj = message_obj

    def plain_result(self, text):
        return text