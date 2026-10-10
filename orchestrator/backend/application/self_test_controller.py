from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from typing import Callable, Mapping

from application.self_test_stages import (
    SelfTestExecutionContext,
    SelfTestStage,
    StageExecutionOutcome,
)
from application.workflow_compilation_service import WorkflowCompilationService
from domain.execution_ir import ExecutionIR, canonical_json, thaw_json
from domain.persistence import WorkflowVersion
from domain.self_test import (
    FailureDescriptor,
    RepairError,
    RepairRecord,
    SELF_TEST_STAGES,
    SelfTestIdentityError,
    SelfTestPolicy,
    SelfTestRunResult,
    SelfTestStageResult,
    self_test_idempotent_id,
)
from ports.self_test import (
    RepairRequest,
    SelfTestRepositoryPort,
    WorkflowRepairPort,
)
from ports.workflow_engine import CompiledWorkflow


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class StartSelfTestRequest:
    tenant_id: str
    workflow_family_id: str
    workflow_version: int
    execution_ir: ExecutionIR
    workflow: CompiledWorkflow
    operation_key: str
    repair_attempt: int = 0


@dataclass(frozen=True)
class RepairExecutionResult:
    record: RepairRecord
    self_test: SelfTestRunResult | None = None


class SelfTestController:
    """Run S0-S4 in order and persist one auditable release-gate result."""

    def __init__(
        self,
        repository: SelfTestRepositoryPort,
        stages: tuple[SelfTestStage, ...],
        *,
        clock: Clock | None = None,
    ) -> None:
        observed = tuple(stage.stage for stage in stages)
        if observed != SELF_TEST_STAGES:
            raise ValueError(f"stages must be ordered exactly as {SELF_TEST_STAGES}")
        self.repository = repository
        self.stages = stages
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _validate_identity(self, request: StartSelfTestRequest) -> None:
        identity = self.repository.get_workflow_identity(
            request.workflow_family_id,
            request.workflow_version,
        )
        observed = (
            request.tenant_id,
            request.execution_ir.workflow_family_id,
            request.execution_ir.workflow_version,
            request.execution_ir.registry_snapshot_id,
            request.execution_ir.content_hash(),
            request.workflow.source_ir_hash,
        )
        expected = (
            identity.tenant_id,
            identity.workflow_family_id,
            identity.workflow_version,
            identity.registry_snapshot_id,
            identity.ir_hash,
            identity.ir_hash,
        )
        if observed != expected:
            raise SelfTestIdentityError(
                "Self-Test input does not match persisted Workflow Version identity"
            )

    @staticmethod
    def _stage_result(
        stage: str,
        started_at: datetime,
        completed_at: datetime,
        outcome: StageExecutionOutcome,
    ) -> SelfTestStageResult:
        probe = outcome.probe
        duration = max(0, int((completed_at - started_at).total_seconds() * 1000))
        if probe.passed:
            return SelfTestStageResult(
                stage,
                "PASS",
                started_at,
                completed_at,
                duration,
                message=probe.message,
                evidence=probe.evidence,
                evidence_refs=probe.evidence_refs,
            )
        descriptor = FailureDescriptor(
            stage,
            str(probe.error_code),
            probe.rule_id,
            probe.node_id,
            probe.agent_id,
            probe.normalized_error_class,
        )
        return SelfTestStageResult(
            stage,
            "FAIL",
            started_at,
            completed_at,
            duration,
            error_code=probe.error_code,
            message=probe.message,
            evidence=probe.evidence,
            evidence_refs=probe.evidence_refs,
            failure_signature=descriptor.signature(),
        )

    def start_self_test(self, request: StartSelfTestRequest) -> SelfTestRunResult:
        if request.repair_attempt < 0:
            raise ValueError("repair_attempt must not be negative")
        self._validate_identity(request)
        started_at = self.clock()
        self_test_id = self_test_idempotent_id(
            request.tenant_id,
            request.workflow_family_id,
            request.workflow_version,
            request.workflow.workflow_hash,
            request.operation_key,
        )
        pending = SelfTestRunResult(
            self_test_id=self_test_id,
            tenant_id=request.tenant_id,
            workflow_family_id=request.workflow_family_id,
            workflow_version=request.workflow_version,
            registry_snapshot_id=request.execution_ir.registry_snapshot_id,
            ir_hash=request.execution_ir.content_hash(),
            workflow_hash=request.workflow.workflow_hash,
            idempotency_key=request.operation_key,
            started_at=started_at,
            status="RUNNING",
            current_stage="S0",
            repair_attempt=request.repair_attempt,
        )
        stored, created = self.repository.begin_self_test(pending)
        if not created:
            return stored

        context = SelfTestExecutionContext(
            request.execution_ir,
            request.workflow,
            request.operation_key,
        )
        results: list[SelfTestStageResult] = []
        failure: SelfTestStageResult | None = None
        for stage in self.stages:
            stage_started = self.clock()
            try:
                outcome = stage.run(context)
            except Exception as error:
                descriptor = FailureDescriptor(
                    stage.stage,
                    "EO-SELFTEST-STAGE",
                    normalized_error_class=type(error).__name__,
                )
                stage_completed = self.clock()
                result = SelfTestStageResult(
                    stage.stage,
                    "FAIL",
                    stage_started,
                    stage_completed,
                    max(
                        0,
                        int((stage_completed - stage_started).total_seconds() * 1000),
                    ),
                    error_code="EO-SELFTEST-STAGE",
                    message="self-test stage raised an unexpected error",
                    evidence={"error_class": type(error).__name__},
                    failure_signature=descriptor.signature(),
                )
            else:
                stage_completed = self.clock()
                result = self._stage_result(
                    stage.stage,
                    stage_started,
                    stage_completed,
                    outcome,
                )
                if result.status == "PASS":
                    if outcome.workflow_reference is not None:
                        context.workflow_reference = outcome.workflow_reference
                    if outcome.canary is not None:
                        context.canary = outcome.canary
            results.append(result)
            if result.status == "FAIL":
                failure = result
                break

        if failure is not None:
            for stage_name in SELF_TEST_STAGES[len(results) :]:
                skipped_at = self.clock()
                results.append(
                    SelfTestStageResult(
                        stage_name,
                        "SKIPPED",
                        skipped_at,
                        skipped_at,
                        0,
                        message=f"skipped because {failure.stage} failed",
                        evidence={"blocked_by": failure.stage},
                    )
                )

        completed_at = self.clock()
        evidence_refs = tuple(
            sorted({reference for item in results for reference in item.evidence_refs})
        )
        completed = replace(
            pending,
            status="FAILED" if failure is not None else "PASSED",
            current_stage=failure.stage if failure is not None else "S4",
            completed_at=completed_at,
            failure_signature=(
                failure.failure_signature if failure is not None else None
            ),
            stage_results=tuple(results),
            evidence_refs=evidence_refs,
        )
        return self.repository.complete_self_test(completed, tuple(results))


class SelfTestRepairService:
    def __init__(
        self,
        repository: SelfTestRepositoryPort,
        controller: SelfTestController,
        compiler: WorkflowCompilationService,
        repair: WorkflowRepairPort,
        policy: SelfTestPolicy,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.controller = controller
        self.compiler = compiler
        self.repair = repair
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _repair_id(self_test_id: str, attempt: int) -> str:
        payload = canonical_json(
            {"self_test_id": self_test_id, "repair_attempt": attempt}
        )
        return "repair-" + hashlib.sha256(payload.encode("ascii")).hexdigest()

    def repair_and_retest(
        self,
        failed_self_test_id: str,
        original: StartSelfTestRequest,
        *,
        operation_key: str,
    ) -> RepairExecutionResult:
        failed = self.repository.get_self_test(failed_self_test_id)
        if failed.status != "FAILED" or failed.failure_signature is None:
            raise RepairError("repair requires a failed Self-Test with a signature")
        if (
            original.workflow_family_id != failed.workflow_family_id
            or original.workflow_version != failed.workflow_version
            or original.workflow.workflow_hash != failed.workflow_hash
        ):
            raise RepairError("repair input differs from the failed Self-Test identity")
        next_attempt = failed.repair_attempt + 1
        if next_attempt > self.policy.max_repair_attempts:
            record = RepairRecord(
                self._repair_id(failed.self_test_id, next_attempt),
                failed.self_test_id,
                failed.workflow_family_id,
                failed.workflow_version,
                next_attempt,
                failed.failure_signature,
                "REPAIR_LIMIT_EXCEEDED",
                self.clock(),
                reason="configured repair attempt limit exceeded",
            )
            return RepairExecutionResult(self.repository.record_repair(record))

        outcome = self.repair.repair(
            RepairRequest(failed, next_attempt, operation_key)
        )
        target_version: int | None = None
        retest_request: StartSelfTestRequest | None = None
        if outcome.status == "RETRY_UNCHANGED":
            retest_request = replace(
                original,
                operation_key=f"{operation_key}:retest",
                repair_attempt=next_attempt,
            )
        elif outcome.status == "NEW_VERSION":
            replacement = outcome.replacement_ir
            if replacement is None:
                raise RepairError("repair adapter omitted replacement IR")
            if (
                replacement.workflow_family_id != failed.workflow_family_id
                or replacement.workflow_version <= failed.workflow_version
            ):
                raise RepairError("replacement IR must be a newer version in the same family")
            compiled = self.compiler.compile(replacement).workflow
            target_version = replacement.workflow_version
            self.repository.create_repair_version(
                WorkflowVersion(
                    replacement.workflow_family_id,
                    replacement.workflow_version,
                    replacement.registry_snapshot_id,
                    replacement.content_hash(),
                    "DRAFT",
                    parent_version=failed.workflow_version,
                )
            )
            retest_request = StartSelfTestRequest(
                original.tenant_id,
                replacement.workflow_family_id,
                replacement.workflow_version,
                replacement,
                compiled,
                f"{operation_key}:retest",
                next_attempt,
            )

        evidence = thaw_json(outcome.evidence)
        if not isinstance(evidence, dict):
            raise TypeError("repair evidence must be an object")
        record = RepairRecord(
            self._repair_id(failed.self_test_id, next_attempt),
            failed.self_test_id,
            failed.workflow_family_id,
            failed.workflow_version,
            next_attempt,
            failed.failure_signature,
            outcome.status,
            self.clock(),
            target_workflow_version=target_version,
            reason=outcome.reason,
            evidence=evidence,
        )
        saved = self.repository.record_repair(record)
        if retest_request is None:
            return RepairExecutionResult(saved)
        return RepairExecutionResult(
            saved,
            self.controller.start_self_test(retest_request),
        )
