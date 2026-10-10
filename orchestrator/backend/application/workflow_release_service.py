from __future__ import annotations

from typing import Mapping

from domain.self_test import ExecuteGateError, PublishGateError
from ports.self_test import SelfTestRepositoryPort
from ports.workflow_engine import (
    CompiledWorkflow,
    WorkflowEnginePort,
    WorkflowExecution,
    WorkflowReference,
)


class WorkflowReleaseService:
    """The only P0-07 path to formal publish and execute side effects."""

    def __init__(
        self,
        repository: SelfTestRepositoryPort,
        engine: WorkflowEnginePort,
    ) -> None:
        self.repository = repository
        self.engine = engine

    def publish(
        self,
        workflow_family_id: str,
        workflow_version: int,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
        *,
        operation_key: str,
    ) -> WorkflowReference:
        identity = self.repository.get_workflow_identity(
            workflow_family_id,
            workflow_version,
        )
        if workflow.source_ir_hash != identity.ir_hash:
            raise PublishGateError("compiled workflow does not match Workflow Version IR")
        if (
            reference.workflow_hash != workflow.workflow_hash
            or reference.source_ir_hash != workflow.source_ir_hash
        ):
            raise PublishGateError("engine workflow reference differs from compiled workflow")
        reservation = self.repository.reserve_publish(
            identity,
            workflow,
            reference,
            operation_key=operation_key,
        )
        if reservation.published:
            return WorkflowReference(
                reservation.engine,
                reservation.engine_workflow_id,
                reservation.ir_hash,
                reservation.workflow_hash,
                True,
                {"idempotent_replay": True},
            )
        published = self.engine.publish_workflow(
            reference,
            operation_key=operation_key,
        )
        if (
            published.workflow_id != reservation.engine_workflow_id
            or published.workflow_hash != reservation.workflow_hash
            or not published.published
        ):
            raise PublishGateError("workflow engine publish response changed reserved identity")
        self.repository.complete_publish(reservation)
        return published

    def execute(
        self,
        workflow_family_id: str,
        workflow_version: int,
        workflow: CompiledWorkflow,
        reference: WorkflowReference,
        inputs: Mapping[str, object],
        *,
        operation_key: str,
    ) -> WorkflowExecution:
        try:
            self.repository.assert_execution_allowed(
                workflow_family_id,
                workflow_version,
                workflow,
                reference,
            )
        except ExecuteGateError:
            raise
        except Exception as error:
            raise ExecuteGateError("formal execution gate rejected the request") from error
        return self.engine.execute_workflow(
            reference,
            inputs,
            operation_key=operation_key,
        )

    def formal_execution_reference(
        self,
        workflow_family_id: str,
        workflow_version: int,
    ) -> WorkflowReference:
        record = self.repository.get_published_release(
            workflow_family_id,
            workflow_version,
        )
        return WorkflowReference(
            record.engine,
            record.engine_workflow_id,
            record.ir_hash,
            record.workflow_hash,
            published=True,
            metadata={"release_gate": "completed"},
        )

    def execute_published(
        self,
        workflow_family_id: str,
        workflow_version: int,
        inputs: Mapping[str, object],
        *,
        operation_key: str,
    ) -> WorkflowExecution:
        reference = self.formal_execution_reference(
            workflow_family_id,
            workflow_version,
        )
        return self.engine.execute_workflow(
            reference,
            inputs,
            operation_key=operation_key,
        )
