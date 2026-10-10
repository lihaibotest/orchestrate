from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(os.environ.get("P0_01_PROJECT_ROOT", Path(__file__).parents[2]))
BACKEND = PROJECT_ROOT / "orchestrator" / "backend"
sys.path.insert(0, str(BACKEND))

from adapters.postgres import PostgresControlPlaneRepository  # noqa: E402
from application.state_transition_service import StateTransitionService  # noqa: E402
from domain.persistence import (  # noqa: E402
    Execution,
    NodeExecution,
    RegistrySnapshot,
    WorkflowFamily,
    WorkflowVersion,
)
from domain.state_machines import (  # noqa: E402
    AggregateNotFoundError,
    EXECUTION_STATE_MACHINE,
    NODE_STATE_MACHINE,
    WORKFLOW_VERSION_STATE_MACHINE,
    InvalidTransitionError,
    StateMachine,
    StateTransition,
    UnknownStateError,
    node_aggregate_id,
    workflow_version_aggregate_id,
)
from infrastructure.logging import JsonFormatter  # noqa: E402
from infrastructure.migrations import PostgresMigrationRunner  # noqa: E402


MATRICES = (
    WORKFLOW_VERSION_STATE_MACHINE,
    EXECUTION_STATE_MACHINE,
    NODE_STATE_MACHINE,
)


class RejectingRepository:
    def transition_execution(
        self,
        execution_id: str,
        to_state: str,
        *,
        reason: str | None = None,
        metadata: object = None,
    ) -> StateTransition:
        raise InvalidTransitionError("EXECUTION", "SUCCEEDED", to_state)


class StateMachineUnitTests(unittest.TestCase):
    def test_transition_matrices_match_design(self) -> None:
        self.assertEqual(
            WORKFLOW_VERSION_STATE_MACHINE.transitions,
            {
                "DRAFT": frozenset({"VALIDATING"}),
                "VALIDATING": frozenset({"SELF_TESTING", "SELF_TEST_FAILED"}),
                "SELF_TESTING": frozenset({"VERIFIED", "SELF_TEST_FAILED"}),
                "VERIFIED": frozenset({"PUBLISHED"}),
                "PUBLISHED": frozenset(),
                "SELF_TEST_FAILED": frozenset({"SELF_TESTING"}),
            },
        )
        self.assertEqual(
            EXECUTION_STATE_MACHINE.transitions,
            {
                "CREATED": frozenset({"RUNNING", "FAILED"}),
                "RUNNING": frozenset({"SUCCEEDED", "FAILED", "PATCHING", "REPLANNING"}),
                "PATCHING": frozenset({"RUNNING", "FAILED"}),
                "REPLANNING": frozenset({"RUNNING", "FAILED"}),
                "SUCCEEDED": frozenset(),
                "FAILED": frozenset(),
            },
        )
        self.assertEqual(
            NODE_STATE_MACHINE.transitions,
            {
                "PENDING": frozenset({"READY"}),
                "READY": frozenset({"RUNNING"}),
                "RUNNING": frozenset({"SUCCEEDED", "RETRY_WAIT", "FAILED"}),
                "RETRY_WAIT": frozenset({"RUNNING", "FAILED"}),
                "SUCCEEDED": frozenset({"EVALUATED", "INVALIDATED"}),
                "EVALUATED": frozenset({"INVALIDATED"}),
                "FAILED": frozenset(),
                "INVALIDATED": frozenset(),
            },
        )

    def test_every_legal_and_illegal_transition(self) -> None:
        for machine in MATRICES:
            states = tuple(machine.transitions)
            for from_state in states:
                for to_state in states:
                    if to_state in machine.transitions[from_state]:
                        transition = machine.transition("aggregate-1", from_state, to_state)
                        self.assertEqual((transition.from_state, transition.to_state), (from_state, to_state))
                    else:
                        with self.assertRaises(InvalidTransitionError):
                            machine.transition("aggregate-1", from_state, to_state)

    def test_terminal_states_cannot_transition(self) -> None:
        expected = {
            "WORKFLOW_VERSION": frozenset({"PUBLISHED"}),
            "EXECUTION": frozenset({"SUCCEEDED", "FAILED"}),
            "NODE": frozenset({"FAILED", "INVALIDATED"}),
        }
        for machine in MATRICES:
            self.assertEqual(machine.terminal_states, expected[machine.aggregate_type])
            for state in machine.terminal_states:
                self.assertTrue(machine.is_terminal(state))

    def test_unknown_states_and_invalid_matrix_are_rejected(self) -> None:
        with self.assertRaises(UnknownStateError):
            EXECUTION_STATE_MACHINE.transition("execution-1", "UNKNOWN", "RUNNING")
        with self.assertRaises(UnknownStateError):
            EXECUTION_STATE_MACHINE.transition("execution-1", "CREATED", "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "undefined destinations"):
            StateMachine("BROKEN", {"A": frozenset({"B"})})

    def test_aggregate_ids_are_unambiguous(self) -> None:
        self.assertNotEqual(
            workflow_version_aggregate_id("family:1", 2),
            workflow_version_aggregate_id("family", 12),
        )
        self.assertEqual(json.loads(node_aggregate_id("execution-1", "node-1")), ["execution-1", "node-1"])

    def test_application_service_audits_rejected_transition(self) -> None:
        service = StateTransitionService(RejectingRepository())  # type: ignore[arg-type]
        with self.assertLogs("application.state_transition_service", level="WARNING") as captured:
            with self.assertRaises(InvalidTransitionError):
                service.transition_execution("execution-1", "RUNNING", reason="duplicate event")
        self.assertIn("state transition rejected", captured.output[0])

        record = logging.LogRecord("test", logging.WARNING, __file__, 1, "rejected", (), None)
        record.aggregate_type = "EXECUTION"
        record.from_state = "SUCCEEDED"
        record.to_state = "RUNNING"
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["aggregate_type"], "EXECUTION")
        self.assertEqual(payload["from_state"], "SUCCEEDED")


@unittest.skipUnless(
    os.getenv("P0_03_POSTGRES_DSN"),
    "set P0_03_POSTGRES_DSN to run PostgreSQL state-machine tests",
)
class StateMachinePostgresTests(unittest.TestCase):
    def test_transactional_transitions_history_and_database_guards(self) -> None:
        import psycopg2

        dsn = os.environ["P0_03_POSTGRES_DSN"]
        schema = f"p003_{uuid4().hex[:12]}"
        repository = PostgresControlPlaneRepository(dsn, schema=schema)
        runner = PostgresMigrationRunner(
            repository._connect,
            PROJECT_ROOT / "orchestrator/migrations",
            schema,
        )
        connection = psycopg2.connect(dsn)
        try:
            self.assertEqual(runner.migrate(), 9)
            repository.create_workflow_family(WorkflowFamily("family-1", "tenant-1", "task-1"))
            repository.create_registry_snapshot(
                RegistrySnapshot("snapshot-1", "family-1", "hash-1", payload={"agents": []})
            )
            repository.create_workflow_version(
                WorkflowVersion("family-1", 1, "snapshot-1", "ir-1", "DRAFT")
            )
            repository.create_execution(
                Execution("execution-1", "family-1", 1, "tenant-1", "CREATED", client_request_id="request-1")
            )
            repository.create_execution(
                Execution("execution-2", "family-1", 1, "tenant-1", "CREATED", client_request_id="request-2")
            )
            repository.create_execution(
                Execution("execution-rollback", "family-1", 1, "tenant-1", "CREATED", client_request_id="request-3")
            )
            repository.create_node_execution(NodeExecution("execution-1", "node-1"))
            repository.create_node_execution(NodeExecution("execution-1", "node-2"))

            workflow_states = ("VALIDATING", "SELF_TESTING", "VERIFIED", "PUBLISHED")
            for state in workflow_states:
                repository.transition_workflow_version(
                    "family-1", 1, state, reason="test workflow path", metadata={"target": state}
                )
            workflow_id = workflow_version_aggregate_id("family-1", 1)
            workflow_history = repository.list_state_transitions("WORKFLOW_VERSION", workflow_id)
            self.assertEqual([item.to_state for item in workflow_history], list(workflow_states))
            self.assertEqual(workflow_history[-1].metadata, {"target": "PUBLISHED"})
            with self.assertRaises(InvalidTransitionError):
                repository.transition_workflow_version("family-1", 1, "VALIDATING")
            self.assertEqual(len(repository.list_state_transitions("WORKFLOW_VERSION", workflow_id)), 4)

            execution_states = ("RUNNING", "PATCHING", "RUNNING", "REPLANNING", "RUNNING", "SUCCEEDED")
            for state in execution_states:
                repository.transition_execution("execution-1", state, reason="test execution path")
            self.assertEqual(
                [item.to_state for item in repository.list_state_transitions("EXECUTION", "execution-1")],
                list(execution_states),
            )
            with self.assertRaises(InvalidTransitionError):
                repository.transition_execution("execution-1", "RUNNING")
            repository.transition_execution("execution-2", "FAILED")
            with self.assertRaises(InvalidTransitionError):
                repository.transition_execution("execution-2", "RUNNING")

            node_states = ("READY", "RUNNING", "RETRY_WAIT", "RUNNING", "SUCCEEDED", "EVALUATED", "INVALIDATED")
            with self.assertRaises(InvalidTransitionError):
                repository.transition_node("execution-1", "node-1", "RUNNING")
            for state in node_states:
                repository.transition_node("execution-1", "node-1", state, reason="test node path")
            node_id = node_aggregate_id("execution-1", "node-1")
            self.assertEqual(
                [item.to_state for item in repository.list_state_transitions("NODE", node_id)],
                list(node_states),
            )
            with self.assertRaises(InvalidTransitionError):
                repository.transition_node("execution-1", "node-1", "READY")
            for state in ("READY", "RUNNING", "FAILED"):
                repository.transition_node("execution-1", "node-2", state)
            with self.assertRaises(InvalidTransitionError):
                repository.transition_node("execution-1", "node-2", "RUNNING")

            with self.assertRaises(AggregateNotFoundError):
                repository.transition_execution("missing-execution", "RUNNING")

            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
                with self.assertRaises(Exception):
                    cursor.execute("UPDATE execution SET status='SUCCEEDED' WHERE execution_id='execution-rollback'")
            connection.rollback()

            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
                cursor.execute(
                    "CREATE FUNCTION reject_transition_history() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'injected history failure'; END; $$"
                )
                cursor.execute(
                    "CREATE TRIGGER reject_transition_history BEFORE INSERT ON state_transition_history "
                    "FOR EACH ROW EXECUTE FUNCTION reject_transition_history()"
                )
            connection.commit()
            with self.assertRaisesRegex(Exception, "injected history failure"):
                repository.transition_execution("execution-rollback", "RUNNING")
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
                cursor.execute("SELECT status FROM execution WHERE execution_id='execution-rollback'")
                self.assertEqual(cursor.fetchone()[0], "CREATED")
                cursor.execute("SELECT count(*) FROM state_transition_history WHERE aggregate_id='execution-rollback'")
                self.assertEqual(cursor.fetchone()[0], 0)
                cursor.execute("DROP TRIGGER reject_transition_history ON state_transition_history")
                cursor.execute("DROP FUNCTION reject_transition_history()")
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
