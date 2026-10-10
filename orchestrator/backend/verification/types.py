from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from contracts.execution_ir_v2 import ExecutionIRV2


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class VerificationError(ValueError):
    def __init__(self, code: str, message: str, *, path: str = "") -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code}: {message}")


def _report(*groups: Iterable[ValidationIssue]) -> ValidationReport:
    return ValidationReport(errors=tuple(issue for group in groups for issue in group))


def verify_ir(
    value: ExecutionIRV2 | dict[str, object],
    *,
    bindings: dict[str, object] | None = None,
    backend: str = "n8n",
) -> ValidationReport:
    try:
        ir = value if isinstance(value, ExecutionIRV2) else ExecutionIRV2.model_validate(value)
    except Exception as error:
        return ValidationReport(errors=(ValidationIssue("IR_SCHEMA_INVALID", "ir", str(error)),))
    from verification.backend import validate_backend
    from verification.graph import validate_graph
    from verification.loops import validate_loops
    from verification.policy import validate_policy

    issues = [
        *validate_graph(ir),
        *validate_loops(ir),
        *validate_policy(ir, bindings or {}),
        *validate_backend(ir, backend=backend),
    ]
    return _report(issues)

