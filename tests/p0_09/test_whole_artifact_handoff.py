from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_artifact import FakeMinioClient, InMemoryArtifactRepository  # noqa: E402
from adapters.minio_artifact import MinioArtifactAdapter  # noqa: E402
from application.execution_controller import ExecutionController  # noqa: E402
from domain.artifact import ProducedArtifact  # noqa: E402
from domain.execution_control import TenantContext  # noqa: E402
from tests.planning_blueprint_v3.test_inbound_adapter import upstream_payload  # noqa: E402
from tests.planning_blueprint_v3.test_production_composition import CompositionFixture  # noqa: E402


class ArtifactDagFixture:
    def __init__(self) -> None:
        self.composition = CompositionFixture(
            capabilities=("case-select", "case-execute")
        )
        self.metadata = InMemoryArtifactRepository()
        self.client = FakeMinioClient()
        self.manager = MinioArtifactAdapter(
            self.client, self.metadata, bucket="artifacts"
        )
        self.controller = ExecutionController(
            self.composition.repository,
            self.composition.release,
            self.manager,
        )
        self.composition.execution = self.controller
        self.composition.generation.execution = self.controller
        self.ingestion = self.composition.ingest(upstream_payload())
        self.view = self.composition.generation.start_formal_execution(
            self.ingestion.blueprint_id,
            tenant_id="tenant-a",
            client_request_id="artifact-run",
            api_idempotency_key="artifact-api-key",
            inputs={"request": "run"},
        )
        self.execution_id = self.view.execution.execution_id

    def start_a(self):  # type: ignore[no-untyped-def]
        self.controller.mark_node_ready(self.execution_id, "select-cases")
        return self.controller.start_next_attempt(self.execution_id, "select-cases")

    @staticmethod
    def output() -> ProducedArtifact:
        return ProducedArtifact(
            "selected-cases",
            b'{"cases":["case-1"]}',
            "application/json",
        )


class WholeArtifactHandoffTests(unittest.TestCase):
    def test_proof_a_agent_success_with_missing_artifact_fails_task(self) -> None:
        fixture = ArtifactDagFixture()
        fixture.start_a()
        result = fixture.controller.complete_agent_execution(
            fixture.execution_id, "select-cases", outputs=()
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.attempt.status, "FAILED")
        self.assertEqual(result.attempt.error_code, "ARTIFACT_MISSING")
        nodes = fixture.composition.repository.nodes
        self.assertEqual(nodes[(fixture.execution_id, "select-cases")].status, "FAILED")
        self.assertNotEqual(nodes[(fixture.execution_id, "select-cases")].status, "SUCCEEDED")
        self.assertEqual(
            fixture.composition.repository.executions[fixture.execution_id].status,
            "FAILED",
        )
        self.assertEqual(
            result.attempt.retry_evidence["artifact_validation"][0]["result"],
            "FAIL",
        )

    def test_proof_b_whole_artifact_ref_makes_downstream_ready(self) -> None:
        fixture = ArtifactDagFixture()
        fixture.start_a()
        completed = fixture.controller.complete_agent_execution(
            fixture.execution_id,
            "select-cases",
            outputs=(fixture.output(),),
        )
        self.assertTrue(completed.succeeded)
        self.assertEqual(completed.attempt.status, "SUCCEEDED")
        self.assertEqual([node.node_id for node in completed.ready_nodes], ["execute-cases"])
        downstream = fixture.composition.repository.nodes[
            (fixture.execution_id, "execute-cases")
        ]
        self.assertEqual(downstream.status, "READY")
        inputs = fixture.controller.resolve_node_inputs(
            TenantContext("tenant-a"), fixture.execution_id, "execute-cases"
        )
        ref = inputs["artifact_binding_0"]
        self.assertEqual(ref.producer_node_id, "select-cases")
        self.assertEqual(ref.validation_status, "PASS")
        self.assertTrue(ref.storage_ref.startswith("artifact-store://"))
        self.assertFalse(hasattr(ref, "content"))

    def test_downstream_cannot_be_ready_before_validation(self) -> None:
        fixture = ArtifactDagFixture()
        fixture.start_a()
        with self.assertRaises(ValueError):
            fixture.controller.mark_node_ready(fixture.execution_id, "execute-cases")
        self.assertEqual(
            fixture.composition.repository.nodes[
                (fixture.execution_id, "execute-cases")
            ].status,
            "PENDING",
        )

    def test_proof_c_technical_retries_keep_one_artifact_identity(self) -> None:
        fixture = ArtifactDagFixture()
        fixture.start_a()
        for expected_retry in (1, 2):
            attempt = fixture.controller.record_technical_failure(
                fixture.execution_id,
                "select-cases",
                error_code="AGENT_NETWORK",
                normalized_error_class="network",
            )
            self.assertEqual((attempt.attempt_no, attempt.retry_count), (1, expected_retry))
            self.assertEqual(len(fixture.metadata.artifacts), 0)
            fixture.controller.resume_technical_retry(
                fixture.execution_id, "select-cases"
            )
        completed = fixture.controller.complete_agent_execution(
            fixture.execution_id,
            "select-cases",
            outputs=(fixture.output(),),
        )
        self.assertTrue(completed.succeeded)
        self.assertEqual((completed.attempt.attempt_no, completed.attempt.retry_count), (1, 2))
        self.assertEqual(len(fixture.metadata.artifacts), 1)
        replay = fixture.manager.put_artifact(
            next_request(fixture, attempt_no=1)
        )
        self.assertEqual(replay.artifact_id, completed.artifacts[0].artifact_id)
        self.assertEqual(len(fixture.metadata.artifacts), 1)

    def test_business_attempt_two_gets_new_artifact_and_preserves_attempt_one(self) -> None:
        fixture = ArtifactDagFixture()
        fixture.start_a()
        first = fixture.controller.complete_agent_execution(
            fixture.execution_id,
            "select-cases",
            outputs=(fixture.output(),),
            complete_node=False,
        )
        second_attempt = fixture.controller.start_next_attempt(
            fixture.execution_id, "select-cases"
        )
        self.assertEqual(second_attempt.attempt_no, 2)
        second = fixture.controller.complete_agent_execution(
            fixture.execution_id,
            "select-cases",
            outputs=(
                ProducedArtifact(
                    "selected-cases",
                    b'{"cases":["case-2"]}',
                    "application/json",
                ),
            ),
        )
        self.assertNotEqual(first.artifacts[0].artifact_id, second.artifacts[0].artifact_id)
        self.assertEqual(len(fixture.metadata.artifacts), 2)
        attempts = fixture.composition.repository.list_attempts(
            fixture.execution_id, "select-cases"
        )
        self.assertEqual([item.attempt_no for item in attempts], [1, 2])


def next_request(fixture: ArtifactDagFixture, *, attempt_no: int):  # type: ignore[no-untyped-def]
    from domain.artifact import ArtifactPutRequest

    execution = fixture.composition.repository.executions[fixture.execution_id]
    definition = fixture.composition.repository.get_runtime_definition(
        execution.workflow_family_id, execution.workflow_version, execution.ir_hash
    )
    node = next(item for item in definition.nodes if item.node_id == "select-cases")
    return ArtifactPutRequest(
        execution.tenant_id,
        execution.workflow_family_id,
        execution.workflow_version,
        execution.execution_id,
        execution.execution_mode,
        "select-cases",
        attempt_no,
        fixture.output(),
        node.expected_outputs[0],
    )


if __name__ == "__main__":
    unittest.main()
