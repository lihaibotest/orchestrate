from __future__ import annotations

from collections.abc import Mapping

from contracts.orchestration import FailureClass


TECHNICAL_CLASSES = {"timeout", "network", "http_429", "http_5xx"}


def classify_failure(*, error: Mapping[str, object] | None = None, exception: BaseException | None = None) -> FailureClass:
    """Normalize transport and agent errors before selecting a retry action."""
    data = dict(error or {})
    status = data.get("http_status", data.get("status_code"))
    if isinstance(status, int):
        if status == 429:
            return "http_429"
        if 500 <= status <= 599:
            return "http_5xx"
    code = str(data.get("error_code", data.get("code", ""))).lower()
    message = str(data.get("message", data.get("error", ""))).lower()
    if "timeout" in code or "timeout" in message or isinstance(exception, TimeoutError):
        return "timeout"
    if any(token in code or token in message for token in ("network", "connection", "dns", "reset")):
        return "network"
    declared = str(data.get("error_class", "")).lower()
    if declared in {"timeout", "network", "http_429", "http_5xx", "business", "quality", "validation", "unknown"}:
        return declared  # type: ignore[return-value]
    return "unknown"


def is_technical_failure(error_class: str) -> bool:
    return error_class in TECHNICAL_CLASSES
