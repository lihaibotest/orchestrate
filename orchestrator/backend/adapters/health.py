from __future__ import annotations

from domain.health import ComponentHealth
from ports.health import HealthProbe
from ports.repositories import RepositoryPort


class RepositoryHealthAdapter(HealthProbe):
    def __init__(self, repository: RepositoryPort) -> None:
        self.repository = repository

    def check(self) -> ComponentHealth:
        try:
            healthy = self.repository.probe()
        except Exception as error:
            return ComponentHealth("persistence", "error", type(error).__name__)
        return ComponentHealth(
            "persistence",
            "ok" if healthy else "error",
            str(self.repository.database_path),
        )
