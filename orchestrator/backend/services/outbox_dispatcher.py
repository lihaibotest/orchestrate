from __future__ import annotations

from collections.abc import Callable


Handler = Callable[[dict[str, object], str], object]


class OutboxDispatcher:
    def __init__(self, handlers: dict[str, Handler]) -> None:
        self.handlers = dict(handlers)

    def dispatch(self, item: dict[str, object]) -> object:
        kind = str(item.get("kind", ""))
        if kind not in self.handlers:
            raise ValueError(f"unsupported outbox kind: {kind}")
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("outbox payload must be an object")
        operation_key = str(item.get("operation_key", ""))
        if not operation_key:
            raise ValueError("outbox operation_key is required")
        return self.handlers[kind](payload, operation_key)

