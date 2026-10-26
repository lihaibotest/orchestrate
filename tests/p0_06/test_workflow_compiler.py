from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Mapping
import unittest


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))

from adapters.fake_agents import InMemoryRegistrySnapshotRepository  # noqa: E402
from adapters.fake_workflow_engine import (  # noqa: E402
    FakeWorkflowCompilerAdapter,
    FakeWorkflowEngineAdapter,
)
from application.execution_ir_service import ExecutionIRValidationService  # noqa: E402
from application.workflow_compilation_service import WorkflowCompilationService  # noqa: E402
from backends.n8n.workflow_compiler import (  # noqa: E402
    N8nCompilerConfig,
    N8nWorkflowCompilerAdapter,
)
from backends.n8n.workflow_engine import N8nWorkflowEngineAdapter  # noqa: E402
from domain.agent_preparation import AgentBinding, build_registry_snapshot  # noqa: E402
from domain.execution_ir import (  # noqa: E402
    AgentBindingRef,
    ArtifactPolicy,
    DataContract,
    ExecutionIR,
    IREdge,
    IRNode,
    RetryPolicy,
)
from domain.execution_ir_validator import ExecutionIRValidationError  # noqa: E402
from ports.workflow_engine import (  # noqa: E402
    CompiledWorkflow,
    WorkflowCompilationError,
    WorkflowEngineOperationConflict,
    WorkflowEnginePort,
    WorkflowEngineResponseError,
)


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


def _snapshot_binding(requirement_id: str, agent_id: str) -> AgentBinding:
    return AgentBinding(
        requirement_id=requirement_id,
        agent_id=agent_id,
        version="1.0.0",
        endpoint=f"https://agents.example.test/{agent_id}/invoke",
        capability=f"capability-{agent_id}",
        input_schema=REQUEST_SCHEMA,
        output_schema=RESULT_SCHEMA,
        credential_ref=f"credential-ref-{agent_id}",
        invocation_policy={"mode": "request_response"},
    )


def _ir_binding(snapshot_id: str, binding: AgentBinding) -> AgentBindingRef:
    return AgentBindingRef(
        registry_snapshot_id=snapshot_id,
        requirement_id=binding.requirement_id,
        agent_id=binding.agent_id,
        version=binding.version,
        endpoint=binding.endpoint,
        capability=binding.capability,
        input_schema=binding.input_schema,
        output_schema=binding.output_schema,
        credential_ref=binding.credential_ref,
    )


def _node(
    node_id: str,
    node_type: str,
    depends_on: tuple[str, ...],
    input_schema: dict[str, object],
    output_schema: dict[str, object],
    binding: AgentBindingRef | None = None,
) -> IRNode:
    return IRNode(
        node_id=node_id,
        name=node_id,
        node_type=node_type,
        depends_on=depends_on,
        input_contract=DataContract(input_schema),
        output_contract=DataContract(output_schema),
        completion_criteria={"status": "completed"},
        artifact_policy=ArtifactPolicy(),
        agent_binding=binding,
        retry_policy=RetryPolicy(
            max_retries=2,
            backoff_seconds=3,
            timeout_seconds=17,
        ),
    )


def _valid_fixture() -> tuple[ExecutionIR, object]:
    bindings = (
        _snapshot_binding("requirement-a", "agent-a"),
        _snapshot_binding("requirement-b", "agent-b"),
    )
    snapshot = build_registry_snapshot(
        "family-compiler",
        bindings,
        snapshot_id="snapshot-compiler",
        created_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )
    node_a = _node(
        "agent-a",
        "invoke_agent",
        (),
        REQUEST_SCHEMA,
        RESULT_SCHEMA,
        _ir_binding(snapshot.snapshot_id, bindings[0]),
    )
    node_b = _node(
        "agent-b",
        "invoke_agent",
        (),
        REQUEST_SCHEMA,
        RESULT_SCHEMA,
        _ir_binding(snapshot.snapshot_id, bindings[1]),
    )
    join = _node(
        "join",
        "join",
        ("agent-a", "agent-b"),
        RESULT_SCHEMA,
        RESULT_SCHEMA,
    )
    end = _node("end", "end", ("join",), RESULT_SCHEMA, {})
    execution_ir = ExecutionIR(
        ir_version="1.0",
        workflow_family_id="family-compiler",
        workflow_version=1,
        task_id="task-compiler",
        registry_snapshot_id=snapshot.snapshot_id,
        nodes=(end, node_b, join, node_a),
        edges=(
            IREdge("join", "end"),
            IREdge("agent-b", "join"),
            IREdge("agent-a", "join"),
        ),
        completion_criteria={"all_nodes": "completed"},
        objective="run smoke workflow",
    )
    return execution_ir, snapshot


def _service(
    execution_ir: ExecutionIR,
    snapshot: object,
    compiler: object,
) -> WorkflowCompilationService:
    repository = InMemoryRegistrySnapshotRepository()
    repository.save_registry_snapshot(snapshot)  # type: ignore[arg-type]
    validation = ExecutionIRValidationService(repository)
    return WorkflowCompilationService(validation, compiler)  # type: ignore[arg-type]


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def validate(
        self,
        artifact: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        self.calls.append(("validate", operation_key, artifact))
        return {"valid": True, "validator": "fake-n8n"}

    def create_inactive(
        self,
        artifact: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        self.calls.append(("import", operation_key, artifact))
        return {"workflow_id": "n8n-workflow-1", "status": "imported"}

    def activate(
        self,
        workflow_id: str,
        *,
        operation_key: str,
    ) -> dict[str, object]:
        self.calls.append(("publish", operation_key, workflow_id))
        return {"workflow_id": workflow_id, "status": "published"}

    def canary(
        self,
        workflow_id: str,
        *,
        operation_key: str,
    ) -> dict[str, object]:
        self.calls.append(("preflight", operation_key, workflow_id))
        return {"workflow_id": workflow_id, "valid": True, "triggerable": True}

    def automatic_start(
        self,
        workflow_id: str,
        payload: dict[str, object],
        *,
        operation_key: str,
    ) -> dict[str, object]:
        self.calls.append(("execute", operation_key, payload))
        return {
            "execution_id": "n8n-execution-1",
            "workflow_id": workflow_id,
            "status": "RUNNING",
        }

    def status(self, execution_id: str) -> dict[str, object]:
        self.calls.append(("get_execution", execution_id, None))
        return {
            "execution_id": execution_id,
            "workflow_id": "n8n-workflow-1",
            "status": "SUCCEEDED",
        }


def _exercise_engine(
    test: unittest.TestCase,
    engine: WorkflowEnginePort,
    workflow: CompiledWorkflow,
) -> None:
    validation = engine.validate_workflow(workflow, operation_key="validate-key")
    test.assertTrue(validation.valid, validation.issues)
    imported = engine.import_workflow(workflow, operation_key="import-key")
    test.assertFalse(imported.published)
    preflight = engine.preflight_workflow(imported, operation_key="preflight-key")
    test.assertTrue(preflight.valid, preflight.issues)
    published = engine.publish_workflow(imported, operation_key="publish-key")
    test.assertTrue(published.published)
    execution = engine.execute_workflow(
        published,
        {"correlation_id": "correlation-1", "request": "run"},
        operation_key="execute-key",
    )
    observed = engine.get_execution(execution.execution_id)
    test.assertEqual(observed.execution_id, execution.execution_id)
    test.assertEqual(observed.workflow_id, published.workflow_id)


class WorkflowCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.execution_ir, self.snapshot = _valid_fixture()
        self.compiler = N8nWorkflowCompilerAdapter(
            N8nCompilerConfig(webhook_credential_ref="webhook-credential-ref")
        )

    def test_application_validates_ir_then_compiles_deterministically(self) -> None:
        service = _service(self.execution_ir, self.snapshot, self.compiler)
        first = service.compile(self.execution_ir)
        second = service.compile(self.execution_ir)
        self.assertTrue(first.ir_validation.ok)
        self.assertEqual(first.workflow, second.workflow)
        self.assertEqual(first.workflow.source_ir_hash, self.execution_ir.content_hash())
        self.assertEqual(len(first.workflow.workflow_hash), 64)
        with self.assertRaises(TypeError):
            first.workflow.document["active"] = True  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            first.workflow.target = "other"  # type: ignore[misc]

    def test_compiler_maps_bindings_retries_connections_and_runtime_fields(self) -> None:
        workflow = self.compiler.compile(self.execution_ir)
        document = workflow.mutable_document()
        nodes = {item["name"]: item for item in document["nodes"]}  # type: ignore[index]
        invoke = nodes["ir::agent-a"]
        self.assertEqual(invoke["type"], "n8n-nodes-base.httpRequest")
        self.assertEqual(invoke["maxTries"], 3)
        self.assertEqual(invoke["waitBetweenTries"], 3000)
        parameters = invoke["parameters"]
        self.assertEqual(parameters["options"]["timeout"], 17000)  # type: ignore[index]
        body = str(parameters["jsonBody"])  # type: ignore[index]
        self.assertIn("={{ { ", body)
        self.assertIn(
            '"test_path":"/app/pytestproject/cases/test_TP82_test/test_TP82_test.py"',
            body,
        )
        self.assertIn(
            '$("orchestrator::start").item.json.body',
            body,
        )
        self.assertNotIn("Object.assign", body)
        self.assertNotIn("...$json", body)
        self.assertNotIn("String(", body)
        self.assertNotIn("Number(", body)
        for field in (
            "correlation_id",
            "workflow_family_id",
            "workflow_version",
            "execution_id",
            "node_id",
            "agent_id",
            "attempt",
            "input_refs",
            "output_contract",
            "idempotency_key",
        ):
            self.assertIn(field, body)
        self.assertEqual(
            invoke["credentials"],
            {"httpHeaderAuth": {"id": "credential-ref-agent-a"}},
        )
        connections = document["connections"]
        roots = connections["orchestrator::start"]["main"][0]  # type: ignore[index]
        self.assertEqual(
            {item["node"] for item in roots},
            {"ir::agent-a", "ir::agent-b"},
        )
        self.assertEqual(
            connections["ir::agent-a"]["main"][0][0]["index"],  # type: ignore[index]
            0,
        )
        self.assertEqual(
            connections["ir::agent-b"]["main"][0][0]["index"],  # type: ignore[index]
            1,
        )

    def test_word_agent_timeout_has_a_two_minute_floor(self) -> None:
        word_node = replace(
            next(node for node in self.execution_ir.nodes if node.node_id == "agent-a"),
            node_id="word_agent",
        )
        compiled = self.compiler._compile_node(self.execution_ir, word_node)
        parameters = compiled["parameters"]
        self.assertEqual(parameters["options"]["timeout"], 120000)  # type: ignore[index]

    def test_word_agent_status_is_sent_to_monitoring_and_task_orchestration(self) -> None:
        agent = next(
            item for item in self.execution_ir.nodes if item.node_id == "agent-a"
        )
        assert agent.agent_binding is not None
        word_binding = replace(agent.agent_binding, agent_id="word_agent")
        word_agent = replace(agent, agent_binding=word_binding)
        execution_ir = replace(
            self.execution_ir,
            nodes=tuple(
                word_agent if item.node_id == "agent-a" else item
                for item in self.execution_ir.nodes
            ),
        )

        document = self.compiler.compile(execution_ir).mutable_document()
        nodes = {item["name"]: item for item in document["nodes"]}  # type: ignore[index]
        running_body = str(
            nodes["monitor::word_agent::running"]["parameters"]["jsonBody"]
        )
        end_body = str(nodes["monitor::word_agent::end"]["parameters"]["jsonBody"])
        task_running = nodes["task-orchestrator::agent-a::running"]
        task_finish = nodes["task-orchestrator::agent-a::finish"]
        task_running_body = str(task_running["parameters"]["jsonBody"])
        task_finish_body = str(task_finish["parameters"]["jsonBody"])

        self.assertIn('agentname: "word_agent"', running_body)
        self.assertIn('status: "running"', running_body)
        self.assertIn('agentname: "word_agent"', end_body)
        self.assertIn('status: "end"', end_body)
        self.assertNotIn("case-generator", running_body)
        self.assertNotIn("case-executor", end_body)
        self.assertEqual(
            task_running["parameters"]["url"],
            "http://172.26.0.1:7000/api/execution/task-status",
        )
        self.assertIn('agentname: "agent-a"', task_running_body)
        self.assertIn('status: "running"', task_running_body)
        self.assertIn("flag: 0", task_running_body)
        self.assertIn('plan_id: "task-compiler"', task_running_body)
        self.assertIn("plan_version: String(1)", task_running_body)
        self.assertIn("round_id: String($execution.id)", task_running_body)
        self.assertIn('agentname: "agent-a"', task_finish_body)
        self.assertIn('status: "finish"', task_finish_body)
        self.assertNotIn('status: "end"', task_finish_body)
        self.assertTrue(task_running["retryOnFail"])
        self.assertEqual(task_running["maxTries"], 3)

        connections = document["connections"]
        self.assertEqual(
            connections["monitor::word_agent::running"]["main"][0][0]["node"],
            "task-orchestrator::agent-a::running",
        )
        self.assertEqual(
            connections["task-orchestrator::agent-a::running"]["main"][0][0]["node"],
            "ir::agent-a",
        )
        self.assertEqual(
            connections["ir::agent-a"]["main"][0][0]["node"],
            "monitor::word_agent::end",
        )
        self.assertEqual(
            connections["monitor::word_agent::end"]["main"][0][0]["node"],
            "task-orchestrator::agent-a::finish",
        )

    def test_workflow_end_monitor_waits_for_execplan_terminals(self) -> None:
        document = self.compiler.compile(self.execution_ir).mutable_document()
        nodes = {item["name"]: item for item in document["nodes"]}  # type: ignore[index]
        monitor = nodes["monitor::workflow::end"]
        body = str(monitor["parameters"]["jsonBody"])
        self.assertIn('agentname: "run smoke workflow"', body)
        self.assertIn('status: "end"', body)
        self.assertIn('plan_id: "task-compiler"', body)
        self.assertIn('plan_version: String(1)', body)
        self.assertIn('round_id: String($execution.id)', body)
        self.assertEqual(
            document["connections"]["ir::end"]["main"][0][0]["node"],  # type: ignore[index]
            "monitor::workflow::end",
        )

    def test_undefined_node_semantics_and_non_http_endpoint_are_blocked(self) -> None:
        join = next(item for item in self.execution_ir.nodes if item.node_id == "join")
        unsupported = replace(join, node_type="wait")
        unsupported_ir = replace(
            self.execution_ir,
            nodes=tuple(
                unsupported if item.node_id == "join" else item
                for item in self.execution_ir.nodes
            ),
        )
        with self.assertRaises(WorkflowCompilationError) as caught:
            self.compiler.compile(unsupported_ir)
        self.assertEqual(caught.exception.issues[0].code, "N8N-NODE-001")

        agent = next(item for item in self.execution_ir.nodes if item.node_id == "agent-a")
        assert agent.agent_binding is not None
        changed_binding = replace(agent.agent_binding, endpoint="agent://agent-a/invoke")
        changed_agent = replace(agent, agent_binding=changed_binding)
        changed_ir = replace(
            self.execution_ir,
            nodes=tuple(
                changed_agent if item.node_id == "agent-a" else item
                for item in self.execution_ir.nodes
            ),
        )
        with self.assertRaises(WorkflowCompilationError) as endpoint_error:
            self.compiler.compile(changed_ir)
        self.assertEqual(endpoint_error.exception.issues[0].code, "N8N-ENDPOINT-001")

    def test_invalid_ir_never_reaches_compiler_port(self) -> None:
        fake = FakeWorkflowCompilerAdapter()
        service = _service(self.execution_ir, self.snapshot, fake)
        unsafe = replace(
            self.execution_ir,
            completion_criteria={"password": "actual-secret"},
        )
        with self.assertRaises(ExecutionIRValidationError):
            service.compile(unsafe)
        self.assertEqual(fake.calls, [])

    def test_domain_application_and_port_are_engine_independent(self) -> None:
        forbidden_imports = {"backends", "adapters", "httpx", "requests", "fastapi"}
        for relative in (
            "orchestrator/backend/domain/execution_ir.py",
            "orchestrator/backend/application/workflow_compilation_service.py",
            "orchestrator/backend/ports/workflow_engine.py",
        ):
            source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports: list[str] = []
            for item in ast.walk(tree):
                if isinstance(item, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in item.names)
                elif isinstance(item, ast.ImportFrom) and item.module:
                    imports.append(item.module.split(".")[0])
            self.assertFalse(forbidden_imports.intersection(imports), relative)
            self.assertNotIn("n8n", source.lower(), relative)


class WorkflowEngineContractTests(unittest.TestCase):
    def test_fake_adapter_satisfies_engine_contract_and_idempotency(self) -> None:
        execution_ir, _snapshot = _valid_fixture()
        workflow = FakeWorkflowCompilerAdapter().compile(execution_ir)
        engine = FakeWorkflowEngineAdapter()
        _exercise_engine(self, engine, workflow)
        first = engine.import_workflow(workflow, operation_key="repeat-import")
        second = engine.import_workflow(workflow, operation_key="repeat-import")
        self.assertEqual(first, second)
        conflicting = CompiledWorkflow("fake", workflow.source_ir_hash, {"other": True})
        with self.assertRaises(WorkflowEngineOperationConflict):
            engine.import_workflow(conflicting, operation_key="repeat-import")
        with self.assertRaises(ValueError):
            engine.validate_workflow(workflow, operation_key=" ")

    def test_n8n_adapter_satisfies_engine_contract_and_forwards_keys(self) -> None:
        execution_ir, _snapshot = _valid_fixture()
        workflow = N8nWorkflowCompilerAdapter(
            N8nCompilerConfig("webhook-credential-ref")
        ).compile(execution_ir)
        gateway = RecordingGateway()
        engine = N8nWorkflowEngineAdapter(gateway)
        _exercise_engine(self, engine, workflow)
        self.assertEqual(
            [(item[0], item[1]) for item in gateway.calls],
            [
                ("validate", "validate-key"),
                ("import", "import-key"),
                ("preflight", "preflight-key"),
                ("publish", "publish-key"),
                ("execute", "execute-key"),
                ("get_execution", "n8n-execution-1"),
            ],
        )

    def test_n8n_adapter_rejects_invalid_engine_responses(self) -> None:
        execution_ir, _snapshot = _valid_fixture()
        workflow = N8nWorkflowCompilerAdapter(
            N8nCompilerConfig("webhook-credential-ref")
        ).compile(execution_ir)

        class MissingIdentifierGateway(RecordingGateway):
            def create_inactive(
                self,
                artifact: dict[str, object],
                *,
                operation_key: str,
            ) -> dict[str, object]:
                return {"status": "imported"}

        engine = N8nWorkflowEngineAdapter(MissingIdentifierGateway())
        with self.assertRaises(WorkflowEngineResponseError):
            engine.import_workflow(workflow, operation_key="import-key")

    def test_compiled_document_contains_references_not_secret_values(self) -> None:
        execution_ir, _snapshot = _valid_fixture()
        workflow = N8nWorkflowCompilerAdapter(
            N8nCompilerConfig("webhook-credential-ref")
        ).compile(execution_ir)
        serialized = json.dumps(workflow.mutable_document(), sort_keys=True)
        self.assertIn("credential-ref-agent-a", serialized)
        self.assertNotIn("actual-secret", serialized)


if __name__ == "__main__":
    unittest.main()
