from __future__ import annotations


def recover_pending_runtime_commands(repository: object) -> list[dict[str, object]]:
    """Return durable pending commands for the configured outbox dispatcher."""
    return [item for item in repository.pending_outbox() if str(item.get("kind", "")).startswith("runtime.")]

