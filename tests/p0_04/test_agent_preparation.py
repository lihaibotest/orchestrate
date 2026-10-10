from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import ast
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))

from adapters.fake_agents import (  # noqa: E402
    FakeAgentRegistryAdapter,
    FakeAgentRuntimeAdapter,
    FakeAgentRuntimeState,
    InMemoryRegistrySnapshotRepository,
)
from adapters.postgres import PostgresControlPlaneRepository  # noqa: E402
from application.agent_preparation_service import AgentPreparationService  # noqa: E402
from domain.agent_preparation import (  # noqa: E402
    AgentCandidate,
    AgentNotFound,
    AgentNotHealthy,
    AgentNotReady,
    AgentRegistrationFailed,
    AgentRequirement,
    AgentStartFailed,
    AgentUnreachable,
    InvalidAgentSchema,
    MissingCredentialRef,
    SnapshotCreationFailed,
)
from domain.persistence import WorkflowFamily, WorkflowVersion  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402


FIXED_TIME = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {"request": {"type": "string"}},
    "required": ["request"],
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}


def candidate(
    *,
    agent_id: str = "agent-1",
    version: str = "1.0.0",
    capability: str = "generate-tests",
    registered: bool = True,
    input_schema: dict[str, object] | None = None,
    output_schema: dict[str, object] | None = None,
    credential_ref: str = "secret://agents/agent-1",
) -> AgentCandidate:
    return AgentCandidate(
        agent_id=agent_id,
        version=version,
        capabilities=(capability,),
        endpoint=f"http://{agent_id}:8080/invoke",
        input_schema=INPUT_SCHEMA if input_schema is None else input_schema,
        output_schema=OUTPUT_SCHEMA if output_schema is None else output_schema,
        credential_ref=credential_ref,
        invocation_policy={"timeout_seconds": 30, "method": "POST"},
        registered=registered,
    )


def requirement() -> AgentRequirement:
    return AgentRequirement("node-generate", "generate-tests")


class FailingSnapshotRepository:
    def save_registry_snapshot(self, snapshot: object) -> object:
        raise RuntimeError("database unavailable")

    def get_registry_snapshot(self, snapshot_id: str) -> object:
        raise KeyError(snapshot_id)


class AgentPreparationUnitTests(unittest.TestCase):
    def test_domain_and_application_do_not_import_external_adapters(self) -> None:
        forbidden = {"adapters", "requests", "httpx", "urllib", "capabilities", "services"}
        for relative in (
            "orchestrator/backend/domain/agent_preparation.py",
            "orchestrator/backend/application/agent_preparation_service.py",
        ):
            tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module.split(".")[0])
            self.assertFalse(forbidden.intersection(imports), relative)

    def service(
        self,
        agent: AgentCandidate,
        *,
        state: FakeAgentRuntimeState | None = None,
        snapshots: InMemoryRegistrySnapshotRepository | None = None,
    ) -> tuple[
        AgentPreparationService,
        FakeAgentRuntimeAdapter,
        InMemoryRegistrySnapshotRepository,
    ]:
        runtime = FakeAgentRuntimeAdapter(
            {agent.identity: state or FakeAgentRuntimeState()}
        )
        repository = snapshots or InMemoryRegistrySnapshotRepository()
        service = AgentPreparationService(
            FakeAgentRegistryAdapter((agent,)),
            runtime,
            repository,
            clock=lambda: FIXED_TIME,
            snapshot_id_factory=lambda: "snapshot-1",
        )
        return service, runtime, repository

    def test_candidate_passes_all_gates_and_snapshot_is_created(self) -> None:
        service, runtime, snapshots = self.service(candidate())
        result = service.prepare_agents("family-1", (requirement(),))
        binding = result.snapshot.bindings[0]
        self.assertEqual(binding.agent_id, "agent-1")
        self.assertEqual(binding.version, "1.0.0")
        self.assertEqual(binding.capability, "generate-tests")
        self.assertEqual(binding.credential_ref, "secret://agents/agent-1")
        self.assertEqual(result.snapshot.hash_algorithm, "sha256")
        self.assertEqual(result.snapshot.created_at, FIXED_TIME)
        self.assertEqual(
            [item[0] for item in runtime.calls],
            ["start", "health", "readiness", "reachability"],
        )
        self.assertIs(snapshots.get_registry_snapshot("snapshot-1"), result.snapshot)

    def test_missing_and_registration_failures_create_no_snapshot(self) -> None:
        snapshots = InMemoryRegistrySnapshotRepository()
        missing = AgentPreparationService(
            FakeAgentRegistryAdapter(()),
            FakeAgentRuntimeAdapter(),
            snapshots,
        )
        with self.assertRaises(AgentNotFound):
            missing.prepare_agents("family-1", (requirement(),))
        unregistered = AgentPreparationService(
            FakeAgentRegistryAdapter((candidate(registered=False),)),
            FakeAgentRuntimeAdapter(),
            snapshots,
        )
        with self.assertRaises(AgentRegistrationFailed):
            unregistered.prepare_agents("family-1", (requirement(),))
        registry_failure = AgentPreparationService(
            FakeAgentRegistryAdapter(failure=RuntimeError("registry offline")),
            FakeAgentRuntimeAdapter(),
            snapshots,
        )
        with self.assertRaises(AgentRegistrationFailed):
            registry_failure.prepare_agents("family-1", (requirement(),))
        self.assertEqual(snapshots.snapshots, {})

    def test_runtime_gate_failures_create_no_snapshot(self) -> None:
        cases = (
            (FakeAgentRuntimeState(start_ok=False), AgentStartFailed),
            (FakeAgentRuntimeState(health_ok=False), AgentNotHealthy),
            (FakeAgentRuntimeState(readiness_ok=False), AgentNotReady),
            (FakeAgentRuntimeState(reachability_ok=False), AgentUnreachable),
        )
        for state, error_type in cases:
            with self.subTest(error_type=error_type.__name__):
                service, _, snapshots = self.service(candidate(), state=state)
                with self.assertRaises(error_type):
                    service.prepare_agents("family-1", (requirement(),))
                self.assertEqual(snapshots.snapshots, {})

    def test_schema_and_credential_failures_create_no_snapshot(self) -> None:
        invalid_candidates = (
            (candidate(input_schema={}), InvalidAgentSchema),
            (candidate(output_schema={"type": "unknown"}), InvalidAgentSchema),
            (candidate(credential_ref=""), MissingCredentialRef),
        )
        for invalid, error_type in invalid_candidates:
            with self.subTest(error_type=error_type.__name__):
                service, _, snapshots = self.service(invalid)
                with self.assertRaises(error_type):
                    service.prepare_agents("family-1", (requirement(),))
                self.assertEqual(snapshots.snapshots, {})

    def test_failed_candidate_falls_back_to_next_eligible_candidate(self) -> None:
        first = candidate(agent_id="agent-a")
        second = candidate(agent_id="agent-b")
        registry = FakeAgentRegistryAdapter((second, first))
        runtime = FakeAgentRuntimeAdapter(
            {
                first.identity: FakeAgentRuntimeState(health_ok=False),
                second.identity: FakeAgentRuntimeState(),
            }
        )
        snapshots = InMemoryRegistrySnapshotRepository()
        service = AgentPreparationService(
            registry,
            runtime,
            snapshots,
            clock=lambda: FIXED_TIME,
            snapshot_id_factory=lambda: "snapshot-fallback",
        )
        result = service.prepare_agents("family-1", (requirement(),))
        self.assertEqual(result.snapshot.bindings[0].agent_id, "agent-b")
        self.assertEqual(result.rejections[0].error_code, "AGENT_NOT_HEALTHY")

    def test_snapshot_hash_is_deterministic_and_version_sensitive(self) -> None:
        snapshots = InMemoryRegistrySnapshotRepository()
        ids = iter(("snapshot-1", "snapshot-2", "snapshot-3"))
        registry = FakeAgentRegistryAdapter((candidate(),))
        service = AgentPreparationService(
            registry,
            FakeAgentRuntimeAdapter(),
            snapshots,
            clock=lambda: FIXED_TIME,
            snapshot_id_factory=lambda: next(ids),
        )
        first = service.prepare_agents("family-1", (requirement(),)).snapshot
        second = service.prepare_agents("family-1", (requirement(),)).snapshot
        self.assertEqual(first.snapshot_hash, second.snapshot_hash)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        registry.candidates = (candidate(version="2.0.0"),)
        third = service.prepare_agents("family-1", (requirement(),)).snapshot
        self.assertNotEqual(first.snapshot_hash, third.snapshot_hash)
        self.assertNotEqual(first.snapshot_id, third.snapshot_id)
        with self.assertRaises(ValueError):
            replace(first, snapshot_hash="g" * 64)

    def test_snapshot_and_nested_binding_content_are_immutable(self) -> None:
        snapshot = self.service(candidate())[0].prepare_agents(
            "family-1", (requirement(),)
        ).snapshot
        with self.assertRaises(TypeError):
            snapshot.bindings[0].input_schema["type"] = "array"  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            snapshot.snapshot_hash = "0" * 64  # type: ignore[misc]

    def test_snapshot_persistence_failure_is_explicit(self) -> None:
        service = AgentPreparationService(
            FakeAgentRegistryAdapter((candidate(),)),
            FakeAgentRuntimeAdapter(),
            FailingSnapshotRepository(),  # type: ignore[arg-type]
        )
        with self.assertRaises(SnapshotCreationFailed):
            service.prepare_agents("family-1", (requirement(),))


@unittest.skipUnless(
    os.getenv("P0_04_POSTGRES_DSN"),
    "set P0_04_POSTGRES_DSN to run PostgreSQL Agent Preparation tests",
)
class AgentPreparationPostgresTests(unittest.TestCase):
    def test_snapshot_persistence_immutability_and_p0_03_integration(self) -> None:
        import psycopg2

        dsn = os.environ["P0_04_POSTGRES_DSN"]
        schema = f"p004_{uuid4().hex[:12]}"
        repository = PostgresControlPlaneRepository(dsn, schema=schema)
        runner = PostgresMigrationRunner(
            repository._connect,
            PROJECT_ROOT / "orchestrator/migrations",
            schema,
        )
        connection = psycopg2.connect(dsn)
        try:
            self.assertEqual(runner.migrate(), 9)
            repository.create_workflow_family(
                WorkflowFamily("family-1", "tenant-1", "task-1")
            )
            registry = FakeAgentRegistryAdapter((candidate(),))
            ids = iter(("snapshot-1", "snapshot-repeat", "snapshot-2"))
            service = AgentPreparationService(
                registry,
                FakeAgentRuntimeAdapter(),
                repository,
                clock=lambda: FIXED_TIME,
                snapshot_id_factory=lambda: next(ids),
            )
            first = service.prepare_agents("family-1", (requirement(),)).snapshot
            loaded = repository.get_registry_snapshot(first.snapshot_id)
            self.assertEqual(loaded.content_payload(), first.content_payload())
            self.assertEqual(loaded.snapshot_hash, first.snapshot_hash)
            repeated = service.prepare_agents("family-1", (requirement(),)).snapshot
            self.assertEqual(repeated.snapshot_id, first.snapshot_id)

            registry.candidates = (candidate(version="2.0.0"),)
            second = service.prepare_agents("family-1", (requirement(),)).snapshot
            self.assertNotEqual(first.snapshot_hash, second.snapshot_hash)

            repository.create_workflow_version(
                WorkflowVersion("family-1", 1, first.snapshot_id, "ir-1", "DRAFT")
            )
            repository.transition_workflow_version("family-1", 1, "VALIDATING")
            repository.transition_workflow_version("family-1", 1, "SELF_TESTING")

            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
                cursor.execute("SELECT status FROM workflow_version WHERE workflow_family_id='family-1' AND version=1")
                self.assertEqual(cursor.fetchone()[0], "SELF_TESTING")
                cursor.execute("SELECT count(*) FROM registry_snapshot")
                self.assertEqual(cursor.fetchone()[0], 2)
                cursor.execute("SELECT count(*) FROM agent_snapshot_binding")
                self.assertEqual(cursor.fetchone()[0], 2)
            connection.commit()

            for statement in (
                "UPDATE agent_snapshot_binding SET endpoint='http://changed' WHERE snapshot_id='snapshot-1'",
                "DELETE FROM agent_snapshot_binding WHERE snapshot_id='snapshot-1'",
                "UPDATE registry_snapshot SET snapshot_hash='changed' WHERE snapshot_id='snapshot-1'",
            ):
                with self.assertRaises(Exception):
                    with connection.cursor() as cursor:
                        cursor.execute(f'SET search_path TO "{schema}"')
                        cursor.execute(statement)
                connection.rollback()

            failed = AgentPreparationService(
                FakeAgentRegistryAdapter((candidate(agent_id="agent-failed", version="3.0.0"),)),
                FakeAgentRuntimeAdapter(
                    {("agent-failed", "3.0.0"): FakeAgentRuntimeState(start_ok=False)}
                ),
                repository,
            )
            with self.assertRaises(AgentStartFailed):
                failed.prepare_agents("family-1", (requirement(),))
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
                cursor.execute("SELECT count(*) FROM registry_snapshot")
                self.assertEqual(cursor.fetchone()[0], 2)
            connection.commit()

            for version in range(9, 0, -1):
                self.assertEqual(runner.migrate(direction="down"), version)
        finally:
            connection.close()
            cleanup = psycopg2.connect(dsn)
            cleanup.autocommit = True
            with cleanup.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cleanup.close()


if __name__ == "__main__":
    unittest.main()
