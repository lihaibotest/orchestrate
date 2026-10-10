from __future__ import annotations

from dataclasses import replace

from domain.execution_ir import ExecutionIR
from domain.persistence import WorkflowFamily, WorkflowVersion
from domain.self_test import (
    ExecuteGateError,
    PublishGateError,
    PublishReservation,
    RepairRecord,
    SELF_TEST_STAGES,
    SelfTestRunResult,
    SelfTestStageResult,
    WorkflowTestIdentity,
)
from domain.state_machines import WORKFLOW_VERSION_STATE_MACHINE, StateTransition
from ports.self_test import (
    ArtifactProbePort,
    CanaryProbePort,
    CanaryProbeResult,
    ObservabilityProbePort,
    ProbeResult,
    RepairOutcome,
    RepairRequest,
    WorkflowRepairPort,
)
from ports.workflow_engine import CompiledWorkflow, WorkflowReference


class InMemorySelfTestRepository:
    def __init__(
        self,
        families: tuple[WorkflowFamily, ...] = (),
        versions: tuple[WorkflowVersion, ...] = (),
    ) -> None:
        self.families = {item.workflow_family_id: item for item in families}
        self.versions = {
            (item.workflow_family_id, item.version): item for item in versions
        }
        self.runs: dict[str, SelfTestRunResult] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self.repairs: dict[str, RepairRecord] = {}
        self.publish_reservations: dict[tuple[str, int], PublishReservation] = {}
        self.transitions: list[StateTransition] = []

    def get_workflow_identity(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> WorkflowTestIdentity:
        try:
            family = self.families[workflow_family_id]
            version = self.versions[(workflow_family_id, workflow_version)]
        except KeyError as error:
            raise KeyError("Workflow Version not found") from error
        return WorkflowTestIdentity(
            family.tenant_id,
            version.workflow_family_id,
            version.version,
            version.registry_snapshot_id,
            version.ir_hash,
            version.status,
            version.parent_version,
        )

    def _transition(self, key: tuple[str, int], to_state: str, reason: str) -> None:
        version = self.versions[key]
        transition = WORKFLOW_VERSION_STATE_MACHINE.transition(
            f'{key[0]}:{key[1]}',
            version.status,
            to_state,
            reason=reason,
        )
        self.versions[key] = replace(version, status=to_state)
        self.transitions.append(transition)

    def begin_self_test(
        self,
        run: SelfTestRunResult,
    ) -> tuple[SelfTestRunResult, bool]:
        idempotency = (run.tenant_id, run.idempotency_key)
        existing_id = self._idempotency.get(idempotency)
        if existing_id is not None:
            return self.runs[existing_id], False
        for existing in self.runs.values():
            if (
                existing.workflow_family_id == run.workflow_family_id
                and existing.workflow_version == run.workflow_version
                and existing.status == "RUNNING"
            ):
                return existing, False
        key = (run.workflow_family_id, run.workflow_version)
        identity = self.get_workflow_identity(*key)
        if identity.tenant_id != run.tenant_id:
            raise ValueError("tenant mismatch")
        if identity.status == "DRAFT":
            self._transition(key, "VALIDATING", "self-test validation started")
            self._transition(key, "SELF_TESTING", "self-test started")
        elif identity.status == "VALIDATING":
            self._transition(key, "SELF_TESTING", "self-test started")
        elif identity.status == "SELF_TEST_FAILED":
            authorized = any(
                item.workflow_family_id == run.workflow_family_id
                and item.source_workflow_version == run.workflow_version
                and item.repair_attempt == run.repair_attempt
                and item.status == "RETRY_UNCHANGED"
                for item in self.repairs.values()
            )
            if run.repair_attempt < 1 or not authorized:
                raise ValueError("failed Workflow Version requires an authorized repair retry")
            self._transition(key, "SELF_TESTING", "authorized self-test retry started")
        else:
            raise ValueError(f"Workflow Version cannot start Self-Test: {identity.status}")
        self.runs[run.self_test_id] = run
        self._idempotency[idempotency] = run.self_test_id
        return run, True

    def complete_self_test(
        self,
        run: SelfTestRunResult,
        stage_results: tuple[SelfTestStageResult, ...],
    ) -> SelfTestRunResult:
        existing = self.runs[run.self_test_id]
        if existing.status != "RUNNING":
            return existing
        if tuple(run.stage_results) != tuple(stage_results):
            raise ValueError("stage result identity mismatch")
        key = (run.workflow_family_id, run.workflow_version)
        self._transition(
            key,
            "VERIFIED" if run.status == "PASSED" else "SELF_TEST_FAILED",
            "self-test completed",
        )
        self.runs[run.self_test_id] = run
        return run

    def get_self_test(self, self_test_id: str) -> SelfTestRunResult:
        try:
            return self.runs[self_test_id]
        except KeyError as error:
            raise KeyError(f"Self-Test not found: {self_test_id}") from error

    def latest_successful_self_test(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> SelfTestRunResult | None:
        candidates = [
            item
            for item in self.runs.values()
            if item.workflow_family_id == workflow_family_id
            and item.workflow_version == workflow_version
            and item.status == "PASSED"
        ]
        return max(candidates, key=lambda item: item.completed_at or item.started_at) if candidates else None

    def record_repair(self, record: RepairRecord) -> RepairRecord:
        existing = self.repairs.get(record.repair_id)
        if existing is not None:
            if existing != record:
                raise ValueError("repair_id is bound to different content")
            return existing
        for item in self.repairs.values():
            if (
                item.self_test_id == record.self_test_id
                and item.repair_attempt == record.repair_attempt
            ):
                if item != record:
                    raise ValueError("repair attempt is already recorded")
                return item
        self.repairs[record.repair_id] = record
        return record

    def create_repair_version(self, version: WorkflowVersion) -> WorkflowVersion:
        if version.parent_version is None:
            raise ValueError("repair version requires parent_version")
        parent_key = (version.workflow_family_id, version.parent_version)
        if parent_key not in self.versions:
            raise KeyError("repair parent Workflow Version not found")
        key = (version.workflow_family_id, version.version)
        existing = self.versions.get(key)
        if existing is not None:
            if existing != version:
                raise ValueError("repair Workflow Version already exists")
            return existing
        self.versions[key] = version
        return version

    @staticmethod
    def _assert_complete_gate(run: SelfTestRunResult | None) -> SelfTestRunResult:
        if run is None or run.status != "PASSED":
            raise PublishGateError("latest successful Self-Test is missing")
        observed = tuple((item.stage, item.status) for item in run.stage_results)
        expected = tuple((stage, "PASS") for stage in SELF_TEST_STAGES)
        if observed != expected:
            raise PublishGateError("Self-Test does not contain S0-S4 PASS")
        return run

    def reserve_publish(
        self,
        identity: WorkflowTestIdentity,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
        *,
        operation_key: str,
    ) -> PublishReservation:
        key = (identity.workflow_family_id, identity.workflow_version)
        current = self.get_workflow_identity(*key)
        existing = self.publish_reservations.get(key)
        if current.status == "PUBLISHED":
            if existing is None:
                raise PublishGateError("published Workflow has no publish record")
            if (
                existing.workflow_hash != workflow.workflow_hash
                or existing.engine_workflow_id != reference.workflow_id
            ):
                raise PublishGateError("repeat publish differs from published identity")
            return replace(existing, published=True)
        if current.status != "VERIFIED":
            raise PublishGateError(f"Workflow status is not VERIFIED: {current.status}")
        run = self._assert_complete_gate(
            self.latest_successful_self_test(*key)
        )
        if (
            run.workflow_hash != workflow.workflow_hash
            or run.registry_snapshot_id != identity.registry_snapshot_id
            or run.ir_hash != identity.ir_hash
        ):
            raise PublishGateError("Self-Test identity is stale")
        reservation = PublishReservation(
            identity.workflow_family_id,
            identity.workflow_version,
            identity.registry_snapshot_id,
            identity.ir_hash,
            workflow.workflow_hash,
            reference.engine,
            reference.workflow_id,
            operation_key,
        )
        if existing is not None and existing != reservation:
            raise PublishGateError("publish is already reserved with different identity")
        self.publish_reservations[key] = reservation
        return reservation

    def complete_publish(self, reservation: PublishReservation) -> PublishReservation:
        key = (reservation.workflow_family_id, reservation.workflow_version)
        existing = self.publish_reservations.get(key)
        if existing is None or existing != reservation:
            raise PublishGateError("publish reservation is missing or changed")
        identity = self.get_workflow_identity(*key)
        if identity.status == "VERIFIED":
            self._transition(key, "PUBLISHED", "release gate passed")
        elif identity.status != "PUBLISHED":
            raise PublishGateError("Workflow left VERIFIED before publish completion")
        completed = replace(reservation, published=True)
        self.publish_reservations[key] = completed
        return completed

    def assert_execution_allowed(
        self,
        workflow_family_id: str,
        workflow_version: int,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
    ) -> None:
        identity = self.get_workflow_identity(workflow_family_id, workflow_version)
        record = self.publish_reservations.get((workflow_family_id, workflow_version))
        if identity.status != "PUBLISHED" or record is None or not record.published:
            raise ExecuteGateError("formal execution requires a published Workflow Version")
        if (
            record.workflow_hash != workflow.workflow_hash
            or record.ir_hash != workflow.source_ir_hash
            or record.engine_workflow_id != reference.workflow_id
            or not reference.published
        ):
            raise ExecuteGateError("formal execution identity differs from published Workflow")

    def get_published_release(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> PublishReservation:
        identity = self.get_workflow_identity(workflow_family_id, workflow_version)
        record = self.publish_reservations.get((workflow_family_id, workflow_version))
        if identity.status != "PUBLISHED" or record is None or not record.published:
            raise ExecuteGateError(
                "formal execution requires a completed matching publish record"
            )
        if (
            record.registry_snapshot_id != identity.registry_snapshot_id
            or record.ir_hash != identity.ir_hash
        ):
            raise ExecuteGateError("published release identity is stale")
        return record


class FakeAgentBindingProbe:
    def __init__(self, result: ProbeResult | None = None) -> None:
        self.result = result or ProbeResult(
            True,
            evidence={"snapshot_match": True, "bindings_checked": True, "reachable": True},
        )
        self.calls: list[str] = []

    def probe(self, execution_ir: ExecutionIR) -> ProbeResult:
        self.calls.append(execution_ir.registry_snapshot_id)
        return self.result


class FakeCanaryProbe(CanaryProbePort):
    def __init__(self, result: CanaryProbeResult | None = None) -> None:
        self.result = result or CanaryProbeResult(
            ProbeResult(
                True,
                evidence={
                    "execution_mode": "SELF_TEST",
                    "agent_invoked": True,
                    "input_contract_valid": True,
                    "output_contract_valid": True,
                    "credential_policy_valid": True,
                },
                evidence_refs=("evidence://canary/default",),
            ),
            "canary-execution-1",
        )
        self.calls: list[tuple[str, str]] = []

    def run(
        self,
        workflow: WorkflowReference,
        execution_ir: ExecutionIR,
        *,
        operation_key: str,
    ) -> CanaryProbeResult:
        self.calls.append((workflow.workflow_id, operation_key))
        return self.result


class FakeArtifactProbe(ArtifactProbePort):
    def __init__(self, result: ProbeResult | None = None) -> None:
        self.result = result or ProbeResult(
            True,
            evidence={"write_ok": True, "content_hash": "sha256:canary"},
            evidence_refs=("artifact://self-test/default",),
        )
        self.calls: list[str] = []

    def probe(
        self,
        canary: CanaryProbeResult,
        execution_ir: ExecutionIR,
    ) -> ProbeResult:
        self.calls.append(str(canary.execution_id))
        return self.result


class FakeObservabilityProbe(ObservabilityProbePort):
    def __init__(self, result: ProbeResult | None = None) -> None:
        self.result = result or ProbeResult(
            True,
            evidence={
                "execution_status_readable": True,
                "node_result_present": True,
                "completion_satisfied": True,
                "monitoring_evidence_present": True,
                "terminal_signal_present": True,
            },
            evidence_refs=("evidence://monitor/default",),
        )
        self.calls: list[str] = []

    def probe(
        self,
        canary: CanaryProbeResult,
        execution_ir: ExecutionIR,
    ) -> ProbeResult:
        self.calls.append(str(canary.execution_id))
        return self.result


class FakeWorkflowRepairAdapter(WorkflowRepairPort):
    def __init__(self, outcomes: tuple[RepairOutcome, ...]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[RepairRequest] = []

    def repair(self, request: RepairRequest) -> RepairOutcome:
        self.calls.append(request)
        if not self.outcomes:
            return RepairOutcome("NO_REPAIR", "no configured repair")
        return self.outcomes.pop(0)
