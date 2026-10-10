from __future__ import annotations

from dataclasses import dataclass

from application.execution_ir_service import ExecutionIRValidationService
from domain.execution_ir import ExecutionIR
from domain.execution_ir_validator import IRValidationReport
from ports.workflow_engine import CompiledWorkflow, WorkflowCompilerPort
from ports.execution_control import ExecutionRepositoryPort
from domain.execution_control import runtime_definition_from_ir


@dataclass(frozen=True)
class WorkflowCompilationResult:
    workflow: CompiledWorkflow
    ir_validation: IRValidationReport


class WorkflowCompilationService:
    """Validate the platform-neutral IR before delegating compilation to a Port."""

    def __init__(
        self,
        ir_validation: ExecutionIRValidationService,
        compiler: WorkflowCompilerPort,
        runtime_definitions: ExecutionRepositoryPort | None = None,
    ) -> None:
        self.ir_validation = ir_validation
        self.compiler = compiler
        self.runtime_definitions = runtime_definitions

    def compile(self, execution_ir: ExecutionIR) -> WorkflowCompilationResult:
        validated = self.ir_validation.validate_or_raise(execution_ir)
        workflow = self.compiler.compile(execution_ir)
        if self.runtime_definitions is not None:
            self.runtime_definitions.save_runtime_definition(
                runtime_definition_from_ir(execution_ir)
            )
        return WorkflowCompilationResult(workflow, validated.report)
