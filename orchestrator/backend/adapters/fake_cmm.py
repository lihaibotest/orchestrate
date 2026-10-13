from __future__ import annotations

from domain.context_event import ContextEvent
from ports.cmm import CMMView


class FakeCMMAdapter:
    def __init__(self, *, available: bool = True, view: str = "") -> None:
        self.available = available
        self.view = view
        self.appended: list[ContextEvent] = []
        self._ids: set[str] = set()

    def get_view(self, context_id: str) -> CMMView:
        if not self.available:
            raise ConnectionError("CMM unavailable")
        return CMMView(self.view)

    def append_event(self, event: ContextEvent) -> bool:
        if not self.available:
            raise ConnectionError("CMM unavailable")
        if event.event_id in self._ids:
            return False
        self._ids.add(event.event_id)
        self.appended.append(event)
        return True
