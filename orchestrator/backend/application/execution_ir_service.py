from __future__ import annotations

from dataclasses import dataclass

from domain.execution_ir import ExecutionIR
from domain.execution_ir_validator import (
    ExecutionIRValidationError,
    ExecutionIRValidator,
    IRValidationIssue,
    IRValidationReport,
)
from ports.agent_preparation import RegistrySnapshotRepositoryPort


@dataclass(frozen=True)
class ExecutionIRValidationResult:
    execution_ir: ExecutionIR
    report: IRValidationReport
    ir_hash: str | None


class ExecutionIRValidationService:
    def __init__(
        self,
        snapshots: RegistrySnapshotRepositoryPort,
        validator: ExecutionIRValidator | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.validator = validator or ExecutionIRValidator()

    def validate(self, execution_ir: ExecutionIR) -> ExecutionIRValidationResult:
        try:
            snapshot = self.snapshots.get_registry_snapshot(
                execution_ir.registry_snapshot_id
            )
        except KeyError:
            report = IRValidationReport(
                (
                    IRValidationIssue(
                        "IR-BIND-001",
                        "registry_snapshot_id",
                        "Registry Snapshot does not exist",
                    ),
                )
            )
            return ExecutionIRValidationResult(execution_ir, report, None)
        report = self.validator.validate(execution_ir, snapshot)
        return ExecutionIRValidationResult(
            execution_ir,
            report,
            execution_ir.content_hash() if report.ok else None,
        )

    def validate_or_raise(self, execution_ir: ExecutionIR) -> ExecutionIRValidationResult:
        result = self.validate(execution_ir)
        if not result.report.ok:
            raise ExecutionIRValidationError(result.report)
        return result
