from __future__ import annotations

from typing import Protocol

from domain.health import ComponentHealth


class HealthProbe(Protocol):
    def check(self) -> ComponentHealth: ...

