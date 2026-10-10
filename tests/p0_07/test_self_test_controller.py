from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.fake_agents import InMemoryRegistrySnapshotRepository  # noqa: E402
from adapters.fake_self_test import (  # noqa: E402
    FakeAgentBindingProbe,
    FakeArtifactProbe,
    FakeCanaryProbe,
    FakeObservabilityProbe,
    FakeWorkflowRepairAdapter,
    InMemorySelfTestRepository,
)
from adapters.fake_workflow_engine import (  # noqa: E402
    FakeWorkflowCompilerAdapter,
    FakeWorkflowEngineAdapter,
)
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.self_test_controller import (  # noqa: E402
    SelfTestController,
    SelfTestRepairService,
    StartSelfTestRequest,
)
from application.self_test_stages import (  # noqa: E402
    S0ValidationStage,
    S1BindingStage,
    S2EngineStage,
    S3CanaryArtifactStage,
    S4ObservabilityStage,
)
from application.workflow_compilation_service import WorkflowCompilationService  # noqa: E402
from application.workflow_release_service import WorkflowReleaseService  # noqa: E402
from domain.persistence import WorkflowFamily, WorkflowVersion  # noqa: E402
from domain.self_test import (  # noqa: E402
    ExecuteGateError,
    FailureDescriptor,
    PublishGateError,
    SelfTestPolicy,
)
from infrastructure.config import OrchestratorSettings  # noqa: E402
from ports.self_test import (  # noqa: E402
    CanaryProbeResult,
    ProbeResult,
    RepairOutcome,
)
from ports.workflow_engine import CompiledWorkflow, WorkflowIssue, WorkflowReference  # noqa: E402
from tests.p0_06.test_workflow_compiler import _valid_fixture  # noqa: E402


def failed_probe(code: str, *, rule: str = "rule", category: str = "probe") -> ProbeResult:
    return ProbeResult(
        False,
        error_code=code,
        message="stable test failure",
        rule_id=rule,
        normalized_error_class=category,
        evidence={"summary": code},
    )


class Fixture:
    def __init__(
        self,
        *,
        binding: ProbeResult | None = None,
        canary: CanaryProbeResult | None = None,
        artifact: ProbeResult | None = None,
        observability: ProbeResult | None = None,
        engine: FakeWorkflowEngineAdapter | None = None,
        invalid_ir: bool = False,
        status: str = "DRAFT",
    ) -> None:
        execution_ir, snapshot = _valid_fixture()
        if invalid_ir:
            execution_ir = replace(
                execution_ir,
                completion_criteria={"password": "not-allowed"},
            )
        snapshots = InMemoryRegistrySnapshotRepository()
        snapshots.save_registry_snapshot(snapshot)  # type: ignore[arg-type]
        self.validation = ExecutionIRValidationService(snapshots)
        self.compiler_adapter = FakeWorkflowCompilerAdapter()
        self.compiler = WorkflowCompilationService(self.validation, self.compiler_adapter)
        self.execution_ir = execution_ir
        self.workflow = self.compiler_adapter.compile(execution_ir)
        self.repository = InMemorySelfTestRepository(
            (WorkflowFamily(execution_ir.workflow_family_id, "tenant-1", "task-1"),),
            (
                WorkflowVersion(
                    execution_ir.workflow_family_id,
                    execution_ir.workflow_version,
                    execution_ir.registry_snapshot_id,
                    execution_ir.content_hash(),
                    status,
                ),
            ),
        )
        self.engine = engine or FakeWorkflowEngineAdapter()
        self.binding = FakeAgentBindingProbe(binding)
        self.canary = FakeCanaryProbe(canary)
        self.artifact = FakeArtifactProbe(artifact)
        self.observability = FakeObservabilityProbe(observability)
        self.controller = SelfTestController(
            self.repository,
            (
                S0ValidationStage(self.validation),
                S1BindingStage(self.binding),
                S2EngineStage(self.engine),
                S3CanaryArtifactStage(self.canary, self.artifact),
                S4ObservabilityStage(self.observability),
            ),
        )

    def request(self, key: str = "self-test-1", repair_attempt: int = 0) -> StartSelfTestRequest:
        return StartSelfTestRequest(
            "tenant-1",
            self.execution_ir.workflow_family_id,
            self.execution_ir.workflow_version,
            self.execution_ir,
            self.workflow,
            key,
            repair_attempt,
        )

    def run(self, key: str = "self-test-1"):
        return self.controller.start_self_test(self.request(key))

    def imported(self) -> WorkflowReference:
        return next(iter(self.engine.workflows.values()))[1]


class SelfTestStageTests(unittest.TestCase):
    def test_repair_limit_is_validated_configuration_not_domain_magic(self) -> None:
        with patch.dict(
            os.environ,
            {"ORCHESTRATOR_SELF_TEST_MAX_REPAIR_ATTEMPTS": "7"},
            clear=False,
        ):
            self.assertEqual(
                OrchestratorSettings.from_environment().self_test_max_repair_attempts,
                7,
            )
        with patch.dict(
            os.environ,
            {"ORCHESTRATOR_SELF_TEST_MAX_REPAIR_ATTEMPTS": "-1"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                OrchestratorSettings.from_environment()

    def test_s0_to_s4_pass_and_duplicate_start_is_idempotent(self) -> None:
        fixture = Fixture()
        result = fixture.run()
        replay = fixture.run()
        self.assertEqual(result, replay)
        self.assertEqual(result.status, "PASSED")
        self.assertEqual([item.status for item in result.stage_results], ["PASS"] * 5)
        self.assertEqual(
            fixture.repository.get_workflow_identity("family-compiler", 1).status,
            "VERIFIED",
        )
        self.assertEqual(len(fixture.repository.runs), 1)
        self.assertIn("artifact://self-test/default", result.evidence_refs)

    def test_s0_failure_skips_all_downstream_stages(self) -> None:
        fixture = Fixture(invalid_ir=True)
        result = fixture.run()
        self.assertEqual([item.status for item in result.stage_results], ["FAIL"] + ["SKIPPED"] * 4)
        self.assertEqual(fixture.binding.calls, [])
        self.assertEqual(fixture.engine.calls, [])

    def test_s1_binding_snapshot_and_reachability_failures_stop_s2(self) -> None:
        for code in ("BINDING_MISMATCH", "SNAPSHOT_MISMATCH", "AGENT_UNREACHABLE"):
            with self.subTest(code=code):
                fixture = Fixture(binding=failed_probe(code))
                result = fixture.run(code)
                self.assertEqual([item.status for item in result.stage_results], ["PASS", "FAIL", "SKIPPED", "SKIPPED", "SKIPPED"])
                self.assertEqual(fixture.engine.calls, [])

    def test_s2_validate_import_and_preflight_failures_stop_s3(self) -> None:
        engines = (
            FakeWorkflowEngineAdapter(validation_issues=(WorkflowIssue("INVALID", "workflow", "bad"),)),
            FakeWorkflowEngineAdapter(import_failure=RuntimeError("import unavailable")),
            FakeWorkflowEngineAdapter(preflight_issues=(WorkflowIssue("NOT_TRIGGERABLE", "trigger", "bad"),)),
        )
        for index, engine in enumerate(engines):
            with self.subTest(index=index):
                fixture = Fixture(engine=engine)
                result = fixture.run(f"s2-{index}")
                self.assertEqual([item.status for item in result.stage_results], ["PASS", "PASS", "FAIL", "SKIPPED", "SKIPPED"])
                self.assertEqual(fixture.canary.calls, [])

    def test_s3_invocation_and_artifact_failures_skip_s4(self) -> None:
        failures = (
            {"canary": CanaryProbeResult(failed_probe("AGENT_INVOCATION_FAILED"))},
            {"artifact": failed_probe("ARTIFACT_WRITE_FAILED")},
        )
        for index, kwargs in enumerate(failures):
            with self.subTest(index=index):
                fixture = Fixture(**kwargs)  # type: ignore[arg-type]
                result = fixture.run(f"s3-{index}")
                self.assertEqual([item.status for item in result.stage_results], ["PASS", "PASS", "PASS", "FAIL", "SKIPPED"])
                self.assertEqual(fixture.observability.calls, [])

    def test_s4_monitor_completion_and_terminal_failures_are_audited(self) -> None:
        for code in ("MONITORING_MISSING", "COMPLETION_FAILED", "TERMINAL_SIGNAL_MISSING"):
            with self.subTest(code=code):
                fixture = Fixture(observability=failed_probe(code))
                result = fixture.run(code)
                self.assertEqual([item.status for item in result.stage_results], ["PASS", "PASS", "PASS", "PASS", "FAIL"])
                self.assertEqual(result.status, "FAILED")

    def test_failure_signature_is_deterministic_and_ignores_message_and_time(self) -> None:
        first = FailureDescriptor("S1", "AGENT_UNREACHABLE", "reach", agent_id="agent-a")
        second = FailureDescriptor("S1", "AGENT_UNREACHABLE", "reach", agent_id="agent-a")
        changed = FailureDescriptor("S1", "AGENT_UNREACHABLE", "reach", agent_id="agent-b")
        self.assertEqual(first.signature(), second.signature())
        self.assertNotEqual(first.signature(), changed.signature())


class ReleaseGateTests(unittest.TestCase):
    def test_publish_and_execute_require_complete_current_gate(self) -> None:
        fixture = Fixture()
        fixture.run()
        release = WorkflowReleaseService(fixture.repository, fixture.engine)
        imported = fixture.imported()
        with self.assertRaises(ExecuteGateError):
            release.execute("family-compiler", 1, fixture.workflow, imported, {}, operation_key="early")
        published = release.publish(
            "family-compiler", 1, fixture.workflow, imported, operation_key="publish-1"
        )
        replay = release.publish(
            "family-compiler", 1, fixture.workflow, imported, operation_key="publish-1"
        )
        self.assertTrue(published.published)
        self.assertTrue(replay.published)
        execution = release.execute(
            "family-compiler", 1, fixture.workflow, published, {"request": "run"}, operation_key="execute-1"
        )
        self.assertEqual(execution.status, "RUNNING")

    def test_non_verified_states_and_verified_without_test_are_rejected(self) -> None:
        for status in ("DRAFT", "SELF_TESTING", "SELF_TEST_FAILED", "VERIFIED"):
            with self.subTest(status=status):
                fixture = Fixture(status=status)
                reference = WorkflowReference("fake", "workflow-1", fixture.workflow.source_ir_hash, fixture.workflow.workflow_hash)
                release = WorkflowReleaseService(fixture.repository, fixture.engine)
                with self.assertRaises(PublishGateError):
                    release.publish("family-compiler", 1, fixture.workflow, reference, operation_key=f"publish-{status}")

    def test_stale_workflow_hash_and_wrong_snapshot_are_rejected(self) -> None:
        fixture = Fixture()
        fixture.run()
        release = WorkflowReleaseService(fixture.repository, fixture.engine)
        changed = CompiledWorkflow("fake", fixture.workflow.source_ir_hash, {"changed": True})
        changed_ref = WorkflowReference("fake", "changed", changed.source_ir_hash, changed.workflow_hash)
        with self.assertRaises(PublishGateError):
            release.publish("family-compiler", 1, changed, changed_ref, operation_key="stale")
        version = fixture.repository.versions[("family-compiler", 1)]
        fixture.repository.versions[("family-compiler", 1)] = replace(version, registry_snapshot_id="other")
        with self.assertRaises(PublishGateError):
            release.publish("family-compiler", 1, fixture.workflow, fixture.imported(), operation_key="snapshot")

    def test_formal_execute_rejects_draft_verified_and_failed_but_canary_is_isolated(self) -> None:
        for status in ("DRAFT", "VERIFIED", "SELF_TEST_FAILED"):
            fixture = Fixture(status=status)
            release = WorkflowReleaseService(fixture.repository, fixture.engine)
            reference = WorkflowReference("fake", "workflow-1", fixture.workflow.source_ir_hash, fixture.workflow.workflow_hash)
            with self.assertRaises(ExecuteGateError):
                release.execute("family-compiler", 1, fixture.workflow, reference, {}, operation_key=status)
        canary_fixture = Fixture()
        result = canary_fixture.run("canary")
        self.assertEqual(result.status, "PASSED")
        self.assertEqual(canary_fixture.canary.calls[0][1], "canary:S3:canary")


class RepairLoopTests(unittest.TestCase):
    def _failed_fixture(self) -> tuple[Fixture, object]:
        fixture = Fixture(binding=failed_probe("AGENT_UNREACHABLE"))
        return fixture, fixture.run("initial-failure")

    def test_unchanged_repair_increments_attempt_and_restarts_at_s0(self) -> None:
        fixture, failed = self._failed_fixture()
        repair = SelfTestRepairService(
            fixture.repository,
            fixture.controller,
            fixture.compiler,
            FakeWorkflowRepairAdapter((RepairOutcome("RETRY_UNCHANGED", "transient"),)),
            SelfTestPolicy(2),
        )
        result = repair.repair_and_retest(failed.self_test_id, fixture.request("initial-failure"), operation_key="repair-1")
        self.assertEqual(result.record.repair_attempt, 1)
        self.assertIsNotNone(result.self_test)
        assert result.self_test is not None
        self.assertEqual(result.self_test.repair_attempt, 1)
        self.assertEqual(result.self_test.stage_results[0].stage, "S0")

    def test_failed_version_cannot_retest_without_repair_authorization(self) -> None:
        fixture, _failed = self._failed_fixture()
        with self.assertRaisesRegex(ValueError, "authorized repair retry"):
            fixture.controller.start_self_test(
                fixture.request("bypass-attempt", repair_attempt=1)
            )

    def test_no_repair_failure_and_limit_are_explicit(self) -> None:
        for status in ("NO_REPAIR", "FAILED"):
            fixture, failed = self._failed_fixture()
            repair = SelfTestRepairService(
                fixture.repository,
                fixture.controller,
                fixture.compiler,
                FakeWorkflowRepairAdapter((RepairOutcome(status, "not repaired"),)),
                SelfTestPolicy(1),
            )
            result = repair.repair_and_retest(failed.self_test_id, fixture.request("initial-failure"), operation_key=status)
            self.assertEqual(result.record.status, status)
            self.assertIsNone(result.self_test)

        fixture, failed = self._failed_fixture()
        repair = SelfTestRepairService(
            fixture.repository,
            fixture.controller,
            fixture.compiler,
            FakeWorkflowRepairAdapter(()),
            SelfTestPolicy(0),
        )
        result = repair.repair_and_retest(failed.self_test_id, fixture.request("initial-failure"), operation_key="limit")
        self.assertEqual(result.record.status, "REPAIR_LIMIT_EXCEEDED")

    def test_semantic_repair_creates_child_version_and_retests_from_s0(self) -> None:
        fixture, failed = self._failed_fixture()
        replacement = replace(fixture.execution_ir, workflow_version=2)
        repair = SelfTestRepairService(
            fixture.repository,
            fixture.controller,
            fixture.compiler,
            FakeWorkflowRepairAdapter((RepairOutcome("NEW_VERSION", "semantic repair", replacement),)),
            SelfTestPolicy(2),
        )
        result = repair.repair_and_retest(failed.self_test_id, fixture.request("initial-failure"), operation_key="repair-new")
        self.assertEqual(result.record.target_workflow_version, 2)
        child = fixture.repository.versions[("family-compiler", 2)]
        self.assertEqual(child.parent_version, 1)
        self.assertIsNotNone(result.self_test)
        assert result.self_test is not None
        self.assertEqual(result.self_test.workflow_version, 2)
        self.assertEqual(result.self_test.stage_results[0].stage, "S0")


if __name__ == "__main__":
    unittest.main()
