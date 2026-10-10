from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class MetricsPort(Protocol):
    def inc(self, name: str, value: int = 1) -> None: ...

    def observe(self, name: str, value: float) -> None: ...

    def exposition(self) -> str: ...


class TracerPort(Protocol):
    def start_as_current_span(
        self, name: str, **kwargs: Any
    ) -> AbstractContextManager[object]: ...
