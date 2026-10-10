from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Mapping

from application.workflow_release_service import WorkflowReleaseService
from domain.artifact import (
    ArtifactPutRequest,
    ArtifactRef,
    ArtifactValidationCheck,
    ArtifactValidationResult,
    ProducedArtifact,
    stable_artifact_id,
)
from domain.execution_control import (
    NodeNotInExecutionIR,
    RuntimeObservation,
    TenantContext,
    agent_operation_key,
    deterministic_execution_id,
    failure_signature,
    request_hash,
    scoped_operation_key,
)
from domain.persistence import Execution, NodeAttempt, NodeExecution
from ports.artifact import ArtifactManagerPort
from ports.execution_control import ExecutionRepositoryPort


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartExecutionCommand:
    tenant: TenantContext
    workflow_family_id: str
    workflow_version: int
    client_request_id: str
    api_idempotency_key: str
    inputs: Mapping[str, object]
    correlation_id: str | None = None


@dataclass(frozen=True)
class ExecutionView:
    execution: Execution
    nodes: tuple[NodeExecution, ...]
    attempts: tuple[NodeAttempt, ...]
    idempotent_replay: bool = False


@dataclass(frozen=True)
class NodeArtifactCompletion:
    attempt: NodeAttempt
    artifacts: tuple[ArtifactRef, ...]
    validation_results: tuple[ArtifactValidationResult, ...]
    ready_nodes: tuple[NodeExecution, ...]
    succeeded: bool


class ExecutionController:
    """Own formal Execution, Node, and Business Attempt application semantics."""

    def __init__(
        self,
        repository: ExecutionRepositoryPort,
        release: WorkflowReleaseService,
        artifact_manager: ArtifactManagerPort | None = None,
    ) -> None:
        self.repository = repository
        self.release = release
        self.artifact_manager = artifact_manager

    @staticmethod
    def _required(value: str, name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} must not be empty")
        return normalized

    def start(self, command: StartExecutionCommand) -> ExecutionView:
        family_id = self._required(command.workflow_family_id, "workflow_family_id")
        client_request_id = self._required(
            command.client_request_id, "client_request_id"
        )
        self._required(command.api_idempotency_key, "Idempotency-Key")
        if command.workflow_version < 1:
            raise ValueError("workflow_version must be positive")

        reference = self.release.formal_execution_reference(
            family_id,
            command.workflow_version,
        )
        definition = self.repository.get_runtime_definition(
            family_id,
            command.workflow_version,
            reference.source_ir_hash,
        )
        execution_id = deterministic_execution_id(
            command.tenant.tenant_id,
            family_id,
            command.workflow_version,
            client_request_id,
        )
        correlation_id = (
            command.correlation_id.strip()
            if command.correlation_id and command.correlation_id.strip()
            else f"correlation-{execution_id.removeprefix('execution-')}"
        )
        api_operation_key = scoped_operation_key(
            command.tenant.tenant_id,
            "create-execution",
            family_id,
            command.workflow_version,
            client_request_id,
        )
        engine_operation_key = scoped_operation_key(
            command.tenant.tenant_id,
            "engine-execute",
            family_id,
            command.workflow_version,
            execution_id,
        )
        inputs_hash = request_hash(command.inputs)
        pending = Execution(
            execution_id,
            family_id,
            command.workflow_version,
            command.tenant.tenant_id,
            "CREATED",
            correlation_id,
            client_request_id,
            registry_snapshot_id=definition.registry_snapshot_id,
            ir_hash=definition.ir_hash,
            workflow_hash=reference.workflow_hash,
            execution_mode="PRODUCTION",
            request_hash=inputs_hash,
            operation_key=api_operation_key,
            engine=reference.engine,
            engine_workflow_id=reference.workflow_id,
        )
        stored, created = self.repository.begin_execution(pending, definition)
        if stored.engine_execution_id is None:
            runtime_inputs = {
                **dict(command.inputs),
                "execution_mode": "PRODUCTION",
                "tenant_id": stored.tenant_id,
                "correlation_id": stored.correlation_id,
                "workflow_family_id": stored.workflow_family_id,
                "workflow_version": stored.workflow_version,
                "execution_id": stored.execution_id,
                "attempt": 1,
                "idempotency_key": engine_operation_key,
            }
            engine_execution = self.release.execute_published(
                family_id,
                command.workflow_version,
                runtime_inputs,
                operation_key=engine_operation_key,
            )
            stored = self.repository.bind_engine_execution(
                stored.execution_id,
                engine_execution_id=engine_execution.execution_id,
                engine_status=engine_execution.status,
            )
        logger.info(
            "formal execution resolved",
            extra={
                "tenant_id": stored.tenant_id,
                "workflow_family_id": stored.workflow_family_id,
                "workflow_version": stored.workflow_version,
                "execution_id": stored.execution_id,
                "correlation_id": stored.correlation_id,
                "event": "formal_execution_start",
            },
        )
        view = self.get(command.tenant, stored.execution_id)
        return ExecutionView(
            view.execution,
            view.nodes,
            view.attempts,
            idempotent_replay=not created,
        )

    def get(self, tenant: TenantContext, execution_id: str) -> ExecutionView:
        execution = self.repository.get_execution(tenant.tenant_id, execution_id)
        nodes = self.repository.list_nodes(execution_id)
        attempts = tuple(
            attempt
            for node in nodes
            for attempt in self.repository.list_attempts(execution_id, node.node_id)
        )
        return ExecutionView(execution, nodes, attempts)

    def _runtime_node(self, execution_id: str, node_id: str):  # type: ignore[no-untyped-def]
        execution = self.repository.get_execution_by_id(execution_id)
        definition = self.repository.get_runtime_definition(
            execution.workflow_family_id,
            execution.workflow_version,
            execution.ir_hash,
        )
        node = next((item for item in definition.nodes if item.node_id == node_id), None)
        if node is None:
            raise NodeNotInExecutionIR(
                f"Node not found in runtime definition: {node_id}"
            )
        return execution, definition, node

    def _artifact_for_binding(self, execution: Execution, binding, nodes):  # type: ignore[no-untyped-def]
        if self.artifact_manager is None:
            return None
        source = next(
            (item for item in nodes if item.node_id == binding.source_node_id), None
        )
        if source is None or source.status != "SUCCEEDED" or source.current_attempt is None:
            return None
        artifact_id = stable_artifact_id(
            execution.execution_id,
            source.node_id,
            source.current_attempt,
            binding.source_output_name,
        )
        try:
            artifact = self.artifact_manager.get_artifact_metadata(
                execution.tenant_id, artifact_id
            )
        except KeyError:
            return None
        except Exception:
            return None
        if artifact.validation_status != "PASS" or artifact.status not in {
            "VALIDATED",
            "CONSUMED",
        }:
            return None
        if not self.artifact_manager.check_exists(execution.tenant_id, artifact_id):
            return None
        return artifact

    def _can_mark_ready(self, execution: Execution, runtime_node, nodes) -> bool:  # type: ignore[no-untyped-def]
        by_id = {item.node_id: item for item in nodes}
        if any(
            dependency not in by_id or by_id[dependency].status != "SUCCEEDED"
            for dependency in runtime_node.depends_on
        ):
            return False
        return all(
            self._artifact_for_binding(execution, binding, nodes) is not None
            for binding in runtime_node.input_bindings
        )

    def mark_node_ready(self, execution_id: str, node_id: str) -> NodeExecution:
        execution, _definition, runtime_node = self._runtime_node(execution_id, node_id)
        nodes = self.repository.list_nodes(execution_id)
        if not self._can_mark_ready(execution, runtime_node, nodes):
            raise ValueError("Node dependencies do not have validated Artifact outputs")
        return self.repository.mark_node_ready(execution_id, node_id)

    def start_next_attempt(self, execution_id: str, node_id: str) -> NodeAttempt:
        return self.repository.start_next_attempt(execution_id, node_id)

    def record_technical_failure(
        self,
        execution_id: str,
        node_id: str,
        *,
        error_code: str,
        normalized_error_class: str,
        evidence: Mapping[str, object] | None = None,
    ) -> NodeAttempt:
        execution = self.repository.get_execution_by_id(execution_id)
        definition = self.repository.get_runtime_definition(
            execution.workflow_family_id,
            execution.workflow_version,
            execution.ir_hash,
        )
        node = next((item for item in definition.nodes if item.node_id == node_id), None)
        if node is None:
            raise KeyError(f"Node not found in runtime definition: {node_id}")
        signature = failure_signature(
            error_code,
            node_id=node_id,
            normalized_error_class=normalized_error_class,
        )
        result = self.repository.record_technical_failure(
            execution_id,
            node_id,
            max_retries=node.max_retries,
            error_code=error_code,
            error_signature=signature,
            evidence=evidence or {},
        )
        logger.info(
            "technical failure recorded",
            extra={
                "correlation_id": execution.correlation_id,
                "tenant_id": execution.tenant_id,
                "workflow_family_id": execution.workflow_family_id,
                "workflow_version": execution.workflow_version,
                "execution_id": execution_id,
                "node_id": node_id,
                "agent_id": node.agent_id,
                "attempt_no": result.attempt_no,
                "retry_count": result.retry_count,
                "event": "technical_retry" if result.status == "RETRY_WAIT" else "retry_exhausted",
                "error_code": error_code,
            },
        )
        return result

    def resume_technical_retry(self, execution_id: str, node_id: str) -> NodeAttempt:
        return self.repository.resume_technical_retry(execution_id, node_id)

    def complete_attempt(
        self,
        execution_id: str,
        node_id: str,
        *,
        complete_node: bool = True,
        complete_execution: bool = False,
    ) -> NodeAttempt:
        _execution, _definition, runtime_node = self._runtime_node(execution_id, node_id)
        if runtime_node.expected_outputs:
            return self.complete_agent_execution(
                execution_id,
                node_id,
                outputs=(),
                complete_node=complete_node,
                complete_execution=complete_execution,
            ).attempt
        return self.repository.complete_attempt(
            execution_id,
            node_id,
            complete_node=complete_node,
            complete_execution=complete_execution,
        )

    @staticmethod
    def _validation_evidence(
        results: tuple[ArtifactValidationResult, ...]
    ) -> dict[str, object]:
        return {
            "artifact_validation": [
                {
                    "artifact_id": result.artifact_id,
                    "result": "PASS" if result.passed else "FAIL",
                    "error_code": result.error_code,
                    "failure_reason": result.failure_reason,
                    "checks": [
                        {
                            "validation_type": check.validation_type,
                            "result": "PASS" if check.passed else "FAIL",
                            "error_code": check.error_code,
                            "reason": check.reason,
                        }
                        for check in result.checks
                    ],
                }
                for result in results
            ]
        }

    def _fail_artifact_completion(
        self,
        execution_id: str,
        node_id: str,
        results: tuple[ArtifactValidationResult, ...],
    ) -> NodeArtifactCompletion:
        failed = next((item for item in results if not item.passed), results[0])
        attempt = self.repository.fail_attempt(
            execution_id,
            node_id,
            error_code=failed.error_code or "ARTIFACT_VALIDATION_FAILED",
            error_signature=failure_signature(
                failed.error_code or "ARTIFACT_VALIDATION_FAILED",
                node_id=node_id,
                normalized_error_class="artifact_validation",
            ),
            fail_execution=True,
            evidence=self._validation_evidence(results),
        )
        return NodeArtifactCompletion(attempt, (), results, (), False)

    def complete_agent_execution(
        self,
        execution_id: str,
        node_id: str,
        *,
        outputs: tuple[ProducedArtifact, ...],
        complete_node: bool = True,
        complete_execution: bool = False,
    ) -> NodeArtifactCompletion:
        """Turn successful Agent output into validated Artifacts before task success."""
        execution, definition, runtime_node = self._runtime_node(execution_id, node_id)
        current_node = next(
            item for item in self.repository.list_nodes(execution_id) if item.node_id == node_id
        )
        if current_node.current_attempt is None:
            raise ValueError("Node has no current Business Attempt")
        expected_by_name = {
            item.output_name: item for item in runtime_node.expected_outputs
        }
        produced_by_name = {item.output_name: item for item in outputs}
        if len(produced_by_name) != len(outputs):
            raise ValueError("Agent output names must be unique")
        unexpected = sorted(set(produced_by_name) - set(expected_by_name))
        missing = sorted(
            name
            for name, expected in expected_by_name.items()
            if expected.required and name not in produced_by_name
        )
        contract_failures = tuple(
            ArtifactValidationResult.from_checks(
                stable_artifact_id(
                    execution_id, node_id, current_node.current_attempt, name
                ),
                (
                    ArtifactValidationCheck(
                        "DECLARED_OUTPUT",
                        False,
                        "ARTIFACT_MISSING" if name in missing else "ARTIFACT_UNDECLARED",
                        "required Agent output was not produced"
                        if name in missing
                        else "Agent produced an undeclared output",
                    ),
                ),
            )
            for name in (*missing, *unexpected)
        )
        if contract_failures:
            return self._fail_artifact_completion(
                execution_id, node_id, contract_failures
            )
        if expected_by_name and self.artifact_manager is None:
            unavailable = ArtifactValidationResult.from_checks(
                stable_artifact_id(
                    execution_id,
                    node_id,
                    current_node.current_attempt,
                    next(iter(expected_by_name)),
                ),
                (
                    ArtifactValidationCheck(
                        "ARTIFACT_MANAGER",
                        False,
                        "ARTIFACT_MANAGER_UNAVAILABLE",
                        "Artifact Manager is not configured",
                    ),
                ),
            )
            return self._fail_artifact_completion(execution_id, node_id, (unavailable,))

        identities = []
        validation_results = []
        assert self.artifact_manager is not None or not produced_by_name
        for output_name, output in sorted(produced_by_name.items()):
            expected = expected_by_name[output_name]
            try:
                identity = self.artifact_manager.put_artifact(  # type: ignore[union-attr]
                    ArtifactPutRequest(
                        execution.tenant_id,
                        execution.workflow_family_id,
                        execution.workflow_version,
                        execution.execution_id,
                        execution.execution_mode,
                        node_id,
                        current_node.current_attempt,
                        output,
                        expected,
                    )
                )
                result = self.artifact_manager.validate_artifact(  # type: ignore[union-attr]
                    execution.tenant_id, identity.artifact_id, expected
                )
            except Exception as error:
                result = ArtifactValidationResult.from_checks(
                    stable_artifact_id(
                        execution_id, node_id, current_node.current_attempt, output_name
                    ),
                    (
                        ArtifactValidationCheck(
                            "PUT",
                            False,
                            "ARTIFACT_PUT_FAILED",
                            str(error),
                        ),
                    ),
                )
            else:
                identities.append(identity)
            validation_results.append(result)
        results = tuple(validation_results)
        if any(not item.passed for item in results):
            return self._fail_artifact_completion(execution_id, node_id, results)

        attempt = self.repository.complete_attempt(
            execution_id,
            node_id,
            complete_node=complete_node,
            complete_execution=complete_execution,
        )
        ready_nodes = []
        nodes = self.repository.list_nodes(execution_id)
        if complete_node:
            for candidate in definition.nodes:
                persisted = next(item for item in nodes if item.node_id == candidate.node_id)
                if persisted.status == "PENDING" and self._can_mark_ready(
                    execution, candidate, nodes
                ):
                    ready_nodes.append(
                        self.repository.mark_node_ready(execution_id, candidate.node_id)
                    )
        refs = tuple(
            ArtifactRef.from_identity(
                self.artifact_manager.get_artifact_metadata(  # type: ignore[union-attr]
                    execution.tenant_id, item.artifact_id
                )
            )
            for item in identities
        )
        return NodeArtifactCompletion(attempt, refs, results, tuple(ready_nodes), True)

    def resolve_node_inputs(
        self, tenant: TenantContext, execution_id: str, node_id: str
    ) -> Mapping[str, ArtifactRef]:
        execution = self.repository.get_execution(tenant.tenant_id, execution_id)
        _execution, _definition, runtime_node = self._runtime_node(execution_id, node_id)
        nodes = self.repository.list_nodes(execution_id)
        resolved: dict[str, ArtifactRef] = {}
        for binding in runtime_node.input_bindings:
            artifact = self._artifact_for_binding(execution, binding, nodes)
            if artifact is None or self.artifact_manager is None:
                raise ValueError("WHOLE_ARTIFACT input is not validated and ready")
            consumed = self.artifact_manager.mark_consumed(
                execution.tenant_id, artifact.artifact_id
            )
            resolved[binding.input_name] = ArtifactRef.from_identity(consumed)
        return resolved

    def fail_attempt(
        self,
        execution_id: str,
        node_id: str,
        *,
        error_code: str,
        normalized_error_class: str,
        fail_execution: bool = True,
        evidence: Mapping[str, object] | None = None,
    ) -> NodeAttempt:
        return self.repository.fail_attempt(
            execution_id,
            node_id,
            error_code=error_code,
            error_signature=failure_signature(
                error_code,
                node_id=node_id,
                normalized_error_class=normalized_error_class,
            ),
            fail_execution=fail_execution,
            evidence=evidence,
        )

    def request_recovery_decision(
        self, execution_id: str, decision: str, reason: str
    ) -> Execution:
        return self.repository.request_execution_decision(
            execution_id, decision, self._required(reason, "reason")
        )

    def consume_runtime_observation(
        self, observation: RuntimeObservation
    ) -> object:
        if observation.kind == "TECHNICAL_FAILURE":
            if observation.node_id is None or observation.error_code is None:
                raise ValueError("technical failure requires node_id and error_code")
            return self.record_technical_failure(
                observation.execution_id,
                observation.node_id,
                error_code=observation.error_code,
                normalized_error_class="normalized_runtime_fact",
                evidence=observation.evidence_dict(),
            )
        if observation.kind == "NODE_SUCCEEDED":
            if observation.node_id is None:
                raise ValueError("node success requires node_id")
            return self.complete_attempt(observation.execution_id, observation.node_id)
        if observation.kind == "NODE_FAILED":
            if observation.node_id is None or observation.error_code is None:
                raise ValueError("node failure requires node_id and error_code")
            return self.fail_attempt(
                observation.execution_id,
                observation.node_id,
                error_code=observation.error_code,
                normalized_error_class="normalized_runtime_fact",
            )
        return self.request_recovery_decision(
            observation.execution_id,
            "FAIL",
            observation.error_code or "normalized execution failure",
        )

    @staticmethod
    def agent_invocation_key(
        execution_id: str, node_id: str, attempt_no: int
    ) -> str:
        return agent_operation_key(execution_id, node_id, attempt_no)
