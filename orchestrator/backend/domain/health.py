from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentHealth:
    name: str
    status: str
    detail: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {"name": self.name, "status": self.status}
        if self.detail:
            value["detail"] = self.detail
        return value

