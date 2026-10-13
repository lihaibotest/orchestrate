from __future__ import annotations

from typing import Protocol

from domain.context_event import ContextEvent


class CMMPort(Protocol):
    def get_view(self, context_id: str) -> "CMMView": ...

    def append_event(self, event: ContextEvent) -> bool: ...


class CMMView:
    def __init__(self, text: str = "", *, degraded: bool = False, source: str = "CMM") -> None:
        self.text = text
        self.degraded = degraded
        self.source = source
