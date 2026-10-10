from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))

from adapters.fake_agents import InMemoryRegistrySnapshotRepository  # noqa: E402
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from domain.agent_preparation import (  # noqa: E402
    AgentBinding,
    build_registry_snapshot,
)
from domain.execution_ir import (  # noqa: E402
    AgentBindingRef,
    ArtifactDeclaration,
    ArtifactPolicy,
    CheckpointDeclaration,
    CheckpointPolicy,
    DataContract,
    ExecutionIR,
    IREdge,
    IRNode,
    RecoveryMetadata,
    RetryPolicy,
)
from domain.execution_ir_validator import (  # noqa: E402
    ExecutionIRValidationError,
    ExecutionIRValidator,
)


FIXED_TIME = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
REQUEST_SCHEMA = {
    "type": "object",
    "properties": {"request": {"type": "string"}},
    "required": ["request"],
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}
FINAL_SCHEMA = {
    "type": "object",
    "properties": {"final": {"type": "string"}},
    "required": ["final"],
}


def snapshot_binding(
    requirement_id: str,
    agent_id: str,
    capability: str,
    input_schema: dict[str, object],
    output_schema: dict[str, object],
) -> AgentBinding:
    return AgentBinding(
        requirement_id=requirement_id,
        agent_id=agent_id,
        version="1.0.0",
        endpoint=f"agent://{agent_id}/invoke",
        capability=capability,
        input_schema=input_schema,
        output_schema=output_schema,
        credential_ref=f"credential://agents/{agent_id}",
        invocation_policy={"mode": "request_response", "timeout_seconds": 30},
    )


def registry_snapshot():
    return build_registry_snapshot(
        "family-1",
        (
            snapshot_binding(
                "req-a", "agent-a", "prepare", REQUEST_SCHEMA, RESULT_SCHEMA
            ),
            snapshot_binding(
                "req-b", "agent-b", "finalize", RESULT_SCHEMA, FINAL_SCHEMA
            ),
        ),
        snapshot_id="snapshot-1",
        created_at=FIXED_TIME,
    )


def ir_binding(binding: AgentBinding) -> AgentBindingRef:
    return AgentBindingRef(
        registry_snapshot_id="snapshot-1",
        requirement_id=binding.requirement_id,
        agent_id=binding.agent_id,
        version=binding.version,
        endpoint=binding.endpoint,
        capability=binding.capability,
        input_schema=binding.input_schema,
        output_schema=binding.output_schema,
        credential_ref=binding.credential_ref,
    )


def node(
    node_id: str,
    *,
    node_type: str,
    depends_on: tuple[str, ...],
    input_schema: dict[str, object],
    output_schema: dict[str, object],
    binding: AgentBindingRef | None = None,
    artifact_policy: ArtifactPolicy | None = None,
    checkpoint_policy: CheckpointPolicy | None = None,
) -> IRNode:
    return IRNode(
        node_id=node_id,
        name=node_id,
        node_type=node_type,
        depends_on=depends_on,
        agent_binding=binding,
        input_contract=DataContract(input_schema),
        output_contract=DataContract(output_schema),
        completion_criteria={"status": "completed"},
        artifact_policy=artifact_policy or ArtifactPolicy(),
        checkpoint_policy=checkpoint_policy,
        retry_policy=RetryPolicy(max_retries=2, backoff_seconds=1, timeout_seconds=30),
    )


def valid_ir() -> tuple[ExecutionIR, object]:
    snapshot = registry_snapshot()
    bindings = {item.requirement_id: item for item in snapshot.bindings}
    node_a = node(
        "node-a",
        node_type="invoke_agent",
        depends_on=(),
        input_schema=REQUEST_SCHEMA,
        output_schema=RESULT_SCHEMA,
        binding=ir_binding(bindings["req-a"]),
        artifact_policy=ArtifactPolicy(produces=("artifact-a",), reuse_allowed=True),
    )
    node_b = node(
        "node-b",
        node_type="invoke_agent",
        depends_on=("node-a",),
        input_schema=RESULT_SCHEMA,
        output_schema=FINAL_SCHEMA,
        binding=ir_binding(bindings["req-b"]),
        artifact_policy=ArtifactPolicy(
            produces=("artifact-b",), consumes=("artifact-a",)
        ),
        checkpoint_policy=CheckpointPolicy(
            enabled=True, required_artifacts=("artifact-b",)
        ),
    )
    end = node(
        "end",
        node_type="end",
        depends_on=("node-b",),
        input_schema=FINAL_SCHEMA,
        output_schema={},
    )
    execution_ir = ExecutionIR(
        ir_version="1.0",
        workflow_family_id="family-1",
        workflow_version=1,
        task_id="task-1",
        registry_snapshot_id="snapshot-1",
        context_view_id="context-1",
        nodes=(node_b, end, node_a),
        edges=(IREdge("node-b", "end"), IREdge("node-a", "node-b")),
        artifacts=(
            ArtifactDeclaration(
                "artifact-b",
                "node-b",
                FINAL_SCHEMA,
                False,
                uri="object://family-1/artifact-b",
            ),
            ArtifactDeclaration(
                "artifact-a",
                "node-a",
                RESULT_SCHEMA,
                True,
                uri="object://family-1/artifact-a",
                content_hash="sha256:abc",
                input_fingerprint="input:abc",
                permission_scope="tenant:tenant-1",
            ),
        ),
        checkpoints=(
            CheckpointDeclaration(
                "checkpoint-b",
                "node-b",
                ("artifact-b",),
                True,
                input_fingerprint="input:def",
            ),
        ),
        recovery=RecoveryMetadata(),
        completion_criteria={"all_nodes": "completed"},
    )
    return execution_ir, snapshot


class ExecutionIRTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = ExecutionIRValidator()

    def test_valid_platform_neutral_ir_passes_all_rules(self) -> None:
        execution_ir, snapshot = valid_ir()
        report = self.validator.validate(execution_ir, snapshot)  # type: ignore[arg-type]
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(execution_ir.nodes[0].node_id, "end")
        self.assertEqual(len(execution_ir.content_hash()), 64)

    def test_dag_duplicate_dangling_dependency_and_cycle_are_rejected(self) -> None:
        execution_ir, snapshot = valid_ir()
        node_a = next(item for item in execution_ir.nodes if item.node_id == "node-a")
        duplicate = replace(execution_ir, nodes=(*execution_ir.nodes, node_a))
        self.assertTrue(self.validator.validate(duplicate, snapshot).by_code("IR-DAG-001"))  # type: ignore[arg-type]

        dangling = replace(
            execution_ir,
            edges=(*execution_ir.edges, IREdge("node-a", "missing")),
        )
        self.assertTrue(self.validator.validate(dangling, snapshot).by_code("IR-DAG-001"))  # type: ignore[arg-type]

        node_a_with_cycle = replace(node_a, depends_on=("node-b",))
        cycle = replace(
            execution_ir,
            nodes=tuple(
                node_a_with_cycle if item.node_id == "node-a" else item
                for item in execution_ir.nodes
            ),
            edges=(*execution_ir.edges, IREdge("node-b", "node-a")),
        )
        self.assertTrue(self.validator.validate(cycle, snapshot).by_code("IR-DAG-002"))  # type: ignore[arg-type]

    def test_binding_must_exist_and_match_the_frozen_snapshot(self) -> None:
        execution_ir, snapshot = valid_ir()
        node_a = next(item for item in execution_ir.nodes if item.node_id == "node-a")
        missing = replace(node_a, agent_binding=None)
        missing_ir = replace(
            execution_ir,
            nodes=tuple(missing if item.node_id == "node-a" else item for item in execution_ir.nodes),
        )
        self.assertTrue(self.validator.validate(missing_ir, snapshot).by_code("IR-BIND-001"))  # type: ignore[arg-type]

        assert node_a.agent_binding is not None
        changed = replace(node_a.agent_binding, endpoint="agent://changed/invoke")
        mismatch = replace(node_a, agent_binding=changed)
        mismatch_ir = replace(
            execution_ir,
            nodes=tuple(mismatch if item.node_id == "node-a" else item for item in execution_ir.nodes),
        )
        self.assertTrue(self.validator.validate(mismatch_ir, snapshot).by_code("IR-BIND-002"))  # type: ignore[arg-type]

    def test_schema_incompatibility_and_binding_schema_drift_are_rejected(self) -> None:
        execution_ir, snapshot = valid_ir()
        node_b = next(item for item in execution_ir.nodes if item.node_id == "node-b")
        incompatible_schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }
        changed = replace(node_b, input_contract=DataContract(incompatible_schema))
        changed_ir = replace(
            execution_ir,
            nodes=tuple(changed if item.node_id == "node-b" else item for item in execution_ir.nodes),
        )
        report = self.validator.validate(changed_ir, snapshot)  # type: ignore[arg-type]
        self.assertTrue(report.by_code("IR-SCHEMA-001"))

    def test_reusable_artifact_requires_integrity_and_permission_metadata(self) -> None:
        execution_ir, snapshot = valid_ir()
        artifact = next(item for item in execution_ir.artifacts if item.artifact_id == "artifact-a")
        invalid = replace(
            artifact,
            content_hash=None,
            input_fingerprint=None,
            permission_scope=None,
        )
        changed_ir = replace(
            execution_ir,
            artifacts=tuple(
                invalid if item.artifact_id == "artifact-a" else item
                for item in execution_ir.artifacts
            ),
        )
        self.assertTrue(self.validator.validate(changed_ir, snapshot).by_code("IR-ART-001"))  # type: ignore[arg-type]

        inline = replace(artifact, uri="data:text/plain,artifact-body")
        inline_ir = replace(
            execution_ir,
            artifacts=tuple(
                inline if item.artifact_id == "artifact-a" else item
                for item in execution_ir.artifacts
            ),
        )
        self.assertTrue(self.validator.validate(inline_ir, snapshot).by_code("IR-ART-001"))  # type: ignore[arg-type]

    def test_recovery_root_must_cover_invalidated_nodes_and_reuse_predecessors(self) -> None:
        execution_ir, snapshot = valid_ir()
        changed_ir = replace(
            execution_ir,
            recovery=RecoveryMetadata(
                resume_root="node-b",
                reused_nodes=("end",),
                invalidated_nodes=("node-a", "node-b"),
            ),
        )
        issues = self.validator.validate(changed_ir, snapshot).by_code("IR-REC-001")  # type: ignore[arg-type]
        self.assertGreaterEqual(len(issues), 2)

    def test_plaintext_secret_is_rejected_but_credential_ref_is_allowed(self) -> None:
        execution_ir, snapshot = valid_ir()
        self.assertFalse(self.validator.validate(execution_ir, snapshot).by_code("IR-SEC-001"))  # type: ignore[arg-type]
        unsafe = replace(
            execution_ir,
            completion_criteria={"password": "actual-password"},
        )
        self.assertTrue(self.validator.validate(unsafe, snapshot).by_code("IR-SEC-001"))  # type: ignore[arg-type]
        schema_field = replace(
            execution_ir,
            completion_criteria={
                "schema": {
                    "type": "object",
                    "properties": {"password": {"type": "string"}},
                }
            },
        )
        self.assertFalse(self.validator.validate(schema_field, snapshot).by_code("IR-SEC-001"))  # type: ignore[arg-type]

    def test_hash_ordering_report_and_nested_values_are_deterministic(self) -> None:
        execution_ir, snapshot = valid_ir()
        reordered = replace(
            execution_ir,
            nodes=tuple(reversed(execution_ir.nodes)),
            edges=tuple(reversed(execution_ir.edges)),
            artifacts=tuple(reversed(execution_ir.artifacts)),
        )
        self.assertEqual(execution_ir.content_hash(), reordered.content_hash())
        first = self.validator.validate(execution_ir, snapshot)  # type: ignore[arg-type]
        second = self.validator.validate(reordered, snapshot)  # type: ignore[arg-type]
        self.assertEqual(first, second)
        with self.assertRaises(TypeError):
            execution_ir.completion_criteria["all_nodes"] = "ignored"  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            execution_ir.task_id = "changed"  # type: ignore[misc]

    def test_application_service_loads_snapshot_and_only_hashes_valid_ir(self) -> None:
        execution_ir, snapshot = valid_ir()
        repository = InMemoryRegistrySnapshotRepository()
        repository.save_registry_snapshot(snapshot)  # type: ignore[arg-type]
        service = ExecutionIRValidationService(repository)
        result = service.validate_or_raise(execution_ir)
        self.assertTrue(result.report.ok)
        self.assertEqual(result.ir_hash, execution_ir.content_hash())

        missing = replace(execution_ir, registry_snapshot_id="missing")
        failed = service.validate(missing)
        self.assertFalse(failed.report.ok)
        self.assertIsNone(failed.ir_hash)
        with self.assertRaises(ExecutionIRValidationError):
            service.validate_or_raise(missing)

    def test_domain_and_application_ir_are_platform_independent(self) -> None:
        forbidden_imports = {
            "fastapi",
            "httpx",
            "requests",
            "adapters",
            "backends",
            "services",
            "minio",
        }
        for relative in (
            "orchestrator/backend/domain/execution_ir.py",
            "orchestrator/backend/domain/execution_ir_validator.py",
            "orchestrator/backend/application/execution_ir_service.py",
        ):
            source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports = []
            for item in ast.walk(tree):
                if isinstance(item, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in item.names)
                elif isinstance(item, ast.ImportFrom) and item.module:
                    imports.append(item.module.split(".")[0])
            self.assertFalse(forbidden_imports.intersection(imports), relative)
            self.assertNotIn("n8n", source.lower(), relative)
            self.assertNotIn("cmm", source.lower(), relative)

    def test_contract_is_closed_and_defines_binding_artifact_and_recovery(self) -> None:
        schema = (PROJECT_ROOT / "contracts/execution-ir.schema.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn('$id: execution-ir.v1', schema)
        self.assertIn('additionalProperties: false', schema)
        self.assertIn('registry_snapshot_id', schema)
        self.assertIn('credential_ref', schema)
        self.assertIn('input_fingerprint', schema)
        self.assertIn('invalidated_nodes', schema)
        self.assertNotIn('additionalProperties: true', schema)

    def test_invalid_domain_inputs_fail_before_validation(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(max_retries=-1)
        with self.assertRaises(ValueError):
            node(
                "bad",
                node_type="n8n_http_request",
                depends_on=(),
                input_schema={},
                output_schema={},
            )
        execution_ir, _snapshot = valid_ir()
        with self.assertRaises(ValueError):
            replace(execution_ir, ir_version="2.0")


if __name__ == "__main__":
    unittest.main()
