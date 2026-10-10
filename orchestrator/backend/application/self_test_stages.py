from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from application.execution_ir_service import ExecutionIRValidationService
from domain.execution_ir import ExecutionIR, thaw_json
from ports.self_test import (
    AgentBindingProbePort,
    ArtifactProbePort,
    CanaryProbePort,
    CanaryProbeResult,
    ObservabilityProbePort,
    ProbeResult,
)
from ports.workflow_engine import (
    CompiledWorkflow,
    WorkflowEnginePort,
    WorkflowReference,
)


@dataclass
class SelfTestExecutionContext:
    execution_ir: ExecutionIR
    workflow: CompiledWorkflow
    operation_key: str
    workflow_reference: WorkflowReference | None = None
    canary: CanaryProbeResult | None = None


@dataclass(frozen=True)
class StageExecutionOutcome:
    probe: ProbeResult
    workflow_reference: WorkflowReference | None = None
    canary: CanaryProbeResult | None = None


class SelfTestStage(Protocol):
    stage: str

    def run(self, context: SelfTestExecutionContext) -> StageExecutionOutcome: ...


def _mapping(value: Mapping[str, object]) -> dict[str, object]:
    result = thaw_json(value)
    if not isinstance(result, dict):
        raise TypeError("probe evidence must be an object")
    return result


class S0ValidationStage:
    stage = "S0"

    def __init__(self, validation: ExecutionIRValidationService) -> None:
        self.validation = validation

    def run(self, context: SelfTestExecutionContext) -> StageExecutionOutcome:
        result = self.validation.validate(context.execution_ir)
        evidence = {
            "ir_hash": context.execution_ir.content_hash(),
            "workflow_hash": context.workflow.workflow_hash,
            "issues": [
                {"code": item.code, "path": item.path, "message": item.message}
                for item in result.report.issues
            ],
        }
        if result.report.ok:
            return StageExecutionOutcome(ProbeResult(True, evidence=evidence))
        issue = result.report.issues[0]
        node_id = None
        if issue.path.startswith("nodes."):
            parts = issue.path.split(".")
            node_id = parts[1] if len(parts) > 1 else None
        return StageExecutionOutcome(
            ProbeResult(
                False,
                error_code="EO-SELFTEST-S0",
                message="Execution IR validation failed",
                rule_id=issue.code,
                node_id=node_id,
                normalized_error_class="ir_validation",
                evidence=evidence,
            )
        )


class S1BindingStage:
    stage = "S1"

    def __init__(self, probe: AgentBindingProbePort) -> None:
        self.binding_probe = probe

    def run(self, context: SelfTestExecutionContext) -> StageExecutionOutcome:
        return StageExecutionOutcome(self.binding_probe.probe(context.execution_ir))


class S2EngineStage:
    stage = "S2"

    def __init__(self, engine: WorkflowEnginePort) -> None:
        self.engine = engine

    @staticmethod
    def _failed(
        error_code: str,
        message: str,
        *,
        evidence: Mapping[str, object] | None = None,
        rule_id: str | None = None,
    ) -> StageExecutionOutcome:
        return StageExecutionOutcome(
            ProbeResult(
                False,
                error_code=error_code,
                message=message,
                rule_id=rule_id,
                normalized_error_class="workflow_engine",
                evidence=evidence or {},
            )
        )

    def run(self, context: SelfTestExecutionContext) -> StageExecutionOutcome:
        prefix = f"{context.operation_key}:S2"
        try:
            validation = self.engine.validate_workflow(
                context.workflow,
                operation_key=f"{prefix}:validate",
            )
        except Exception as error:
            return self._failed(
                "EO-N8N-VALIDATE",
                "workflow engine validation call failed",
                evidence={"error_class": type(error).__name__},
            )
        if not validation.valid:
            issue = validation.issues[0]
            return self._failed(
                "EO-N8N-VALIDATE",
                "workflow engine rejected compiled workflow",
                evidence={"issues": [item.code for item in validation.issues]},
                rule_id=issue.code,
            )
        try:
            reference = self.engine.import_workflow(
                context.workflow,
                operation_key=f"{prefix}:import",
            )
        except Exception as error:
            return self._failed(
                "EO-N8N-IMPORT",
                "workflow engine import failed",
                evidence={"error_class": type(error).__name__},
            )
        try:
            preflight = self.engine.preflight_workflow(
                reference,
                operation_key=f"{prefix}:preflight",
            )
        except Exception as error:
            return self._failed(
                "EO-N8N-PREFLIGHT",
                "workflow trigger preflight call failed",
                evidence={
                    "workflow_id": reference.workflow_id,
                    "error_class": type(error).__name__,
                },
            )
        if not preflight.valid:
            issue = preflight.issues[0]
            return self._failed(
                "EO-N8N-PREFLIGHT",
                "workflow trigger is not available",
                evidence={
                    "workflow_id": reference.workflow_id,
                    "issues": [item.code for item in preflight.issues],
                },
                rule_id=issue.code,
            )
        return StageExecutionOutcome(
            ProbeResult(
                True,
                evidence={
                    "workflow_id": reference.workflow_id,
                    "engine": reference.engine,
                    "validated": True,
                    "imported": True,
                    "triggerable": True,
                },
            ),
            workflow_reference=reference,
        )


class S3CanaryArtifactStage:
    stage = "S3"

    def __init__(
        self,
        canary: CanaryProbePort,
        artifact: ArtifactProbePort,
    ) -> None:
        self.canary_probe = canary
        self.artifact_probe = artifact

    def run(self, context: SelfTestExecutionContext) -> StageExecutionOutcome:
        if context.workflow_reference is None:
            return StageExecutionOutcome(
                ProbeResult(
                    False,
                    error_code="EO-CANARY-NO-WORKFLOW",
                    message="S2 did not produce an imported workflow",
                    normalized_error_class="self_test_invariant",
                )
            )
        canary = self.canary_probe.run(
            context.workflow_reference,
            context.execution_ir,
            operation_key=f"{context.operation_key}:S3:canary",
        )
        if not canary.probe.passed:
            return StageExecutionOutcome(canary.probe, canary=canary)
        artifact = self.artifact_probe.probe(canary, context.execution_ir)
        if not artifact.passed:
            return StageExecutionOutcome(artifact, canary=canary)
        evidence = {
            "execution_id": canary.execution_id,
            "execution_mode": "SELF_TEST",
            "canary": _mapping(canary.probe.evidence),
            "artifact": _mapping(artifact.evidence),
        }
        refs = tuple(sorted((*canary.probe.evidence_refs, *artifact.evidence_refs)))
        return StageExecutionOutcome(
            ProbeResult(True, evidence=evidence, evidence_refs=refs),
            canary=canary,
        )


class S4ObservabilityStage:
    stage = "S4"

    def __init__(self, observability: ObservabilityProbePort) -> None:
        self.observability_probe = observability

    def run(self, context: SelfTestExecutionContext) -> StageExecutionOutcome:
        if context.canary is None or not context.canary.probe.passed:
            return StageExecutionOutcome(
                ProbeResult(
                    False,
                    error_code="EO-MON-NO-CANARY",
                    message="S3 did not produce a successful Canary execution",
                    normalized_error_class="self_test_invariant",
                )
            )
        return StageExecutionOutcome(
            self.observability_probe.probe(context.canary, context.execution_ir),
            canary=context.canary,
        )
