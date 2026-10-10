from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from domain.execution_ir import ExecutionIR, freeze_mapping
from domain.persistence import WorkflowVersion
from domain.self_test import (
    PublishReservation,
    RepairRecord,
    SelfTestRunResult,
    SelfTestStageResult,
    WorkflowTestIdentity,
)
from ports.workflow_engine import CompiledWorkflow, WorkflowReference


@dataclass(frozen=True)
class ProbeResult:
    passed: bool
    error_code: str | None = None
    message: str | None = None
    rule_id: str | None = None
    node_id: str | None = None
    agent_id: str | None = None
    normalized_error_class: str = "probe"
    evidence: Mapping[str, object] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence))
        object.__setattr__(self, "evidence_refs", tuple(sorted(self.evidence_refs)))
        if not self.passed and not self.error_code:
            raise ValueError("failed probe requires error_code")


@dataclass(frozen=True)
class CanaryProbeResult:
    probe: ProbeResult
    execution_id: str | None = None

    def __post_init__(self) -> None:
        if self.probe.passed and not (self.execution_id or "").strip():
            raise ValueError("successful Canary probe requires execution_id")


@dataclass(frozen=True)
class RepairRequest:
    failed_run: SelfTestRunResult
    repair_attempt: int
    operation_key: str


@dataclass(frozen=True)
class RepairOutcome:
    status: str
    reason: str
    replacement_ir: ExecutionIR | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"RETRY_UNCHANGED", "NEW_VERSION", "NO_REPAIR", "FAILED"}:
            raise ValueError(f"unsupported repair outcome: {self.status}")
        if self.status == "NEW_VERSION" and self.replacement_ir is None:
            raise ValueError("NEW_VERSION repair requires replacement_ir")
        if self.status != "NEW_VERSION" and self.replacement_ir is not None:
            raise ValueError("only NEW_VERSION repair may contain replacement_ir")
        object.__setattr__(self, "evidence", freeze_mapping(self.evidence))


class AgentBindingProbePort(Protocol):
    def probe(self, execution_ir: ExecutionIR) -> ProbeResult: ...


class CanaryProbePort(Protocol):
    def run(
        self,
        workflow: WorkflowReference,
        execution_ir: ExecutionIR,
        *,
        operation_key: str,
    ) -> CanaryProbeResult: ...


class ArtifactProbePort(Protocol):
    def probe(
        self,
        canary: CanaryProbeResult,
        execution_ir: ExecutionIR,
    ) -> ProbeResult: ...


class ObservabilityProbePort(Protocol):
    def probe(
        self,
        canary: CanaryProbeResult,
        execution_ir: ExecutionIR,
    ) -> ProbeResult: ...


class WorkflowRepairPort(Protocol):
    def repair(self, request: RepairRequest) -> RepairOutcome: ...


class SelfTestRepositoryPort(Protocol):
    def get_workflow_identity(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> WorkflowTestIdentity: ...

    def begin_self_test(
        self,
        run: SelfTestRunResult,
    ) -> tuple[SelfTestRunResult, bool]: ...

    def complete_self_test(
        self,
        run: SelfTestRunResult,
        stage_results: tuple[SelfTestStageResult, ...],
    ) -> SelfTestRunResult: ...

    def get_self_test(self, self_test_id: str) -> SelfTestRunResult: ...

    def latest_successful_self_test(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> SelfTestRunResult | None: ...

    def record_repair(self, record: RepairRecord) -> RepairRecord: ...

    def create_repair_version(self, version: WorkflowVersion) -> WorkflowVersion: ...

    def reserve_publish(
        self,
        identity: WorkflowTestIdentity,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
        *,
        operation_key: str,
    ) -> PublishReservation: ...

    def complete_publish(self, reservation: PublishReservation) -> PublishReservation: ...

    def assert_execution_allowed(
        self,
        workflow_family_id: str,
        workflow_version: int,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
    ) -> None: ...

    def get_published_release(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> PublishReservation: ...
