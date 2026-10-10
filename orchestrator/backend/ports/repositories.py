from __future__ import annotations

from pathlib import Path
from typing import Protocol


class RepositoryPort(Protocol):
    database_path: Path

    def get_session(self, session_id: str) -> dict[str, object] | None: ...

    def probe(self) -> bool: ...
