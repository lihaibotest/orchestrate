from __future__ import annotations

from contracts.execution_ir_v2 import ExecutionIRV2
from verification.types import ValidationIssue


def validate_backend(ir: ExecutionIRV2, *, backend: str) -> tuple[ValidationIssue, ...]:
    if backend != "n8n":
        return (ValidationIssue("BACKEND_UNSUPPORTED", "backend", f"backend {backend!r} is not enabled in 1.1.0"),)
    return ()

