from __future__ import annotations

from typing import Mapping

from domain.agent_preparation import RegistrySnapshot
from domain.execution_ir import (
    AgentBindingRef,
    ArtifactDeclaration,
    ArtifactPolicy,
    DataContract,
    ExecutionIR,
    IREdge,
    IRNode,
    RetryPolicy,
)
from domain.planning_blueprint import CapabilityInvalid, PlanningBlueprintPlan


def _retry_policy(value: Mapping[str, object]) -> RetryPolicy:
    max_retries = value.get("max_retries")
    if max_retries is None and isinstance(value.get("max_attempts"), int):
        max_retries = max(0, int(value["max_attempts"]) - 1)
    return RetryPolicy(
        max_retries=0 if max_retries is None else int(max_retries),
        backoff_seconds=int(value.get("backoff_seconds", 0)),
        timeout_seconds=None if value.get("timeout_seconds") is None else int(value["timeout_seconds"]),
    )


def _artifact_declarations(plan: PlanningBlueprintPlan) -> tuple[ArtifactDeclaration, ...]:
    declarations: list[ArtifactDeclaration] = []
    for task in plan.tasks:
        for name, metadata in sorted(task.outputs.items()):
            details = metadata if isinstance(metadata, Mapping) else {"value": metadata}
            schema = details.get("schema", {"type": "object"})
            if not isinstance(schema, Mapping):
                schema = {"type": "object"}
            declarations.append(
                ArtifactDeclaration(
                    artifact_id=f"{task.task_id}:{name}",
                    producer_node_id=task.task_id,
                    output_name=name,
                    artifact_type=str(details.get("artifact_type", "FILE")),
                    media_type=str(details.get("media_type", "application/octet-stream")),
                    schema_ref=(
                        None
                        if details.get("schema_ref") is None
                        else str(details["schema_ref"])
                    ),
                    schema=schema,
                    required=bool(details.get("required", True)),
                    min_size_bytes=(
                        None
                        if details.get("min_size_bytes") is None
                        else int(details["min_size_bytes"])
                    ),
                    max_size_bytes=(
                        None
                        if details.get("max_size_bytes") is None
                        else int(details["max_size_bytes"])
                    ),
                    reusable=False,
                    uri=None,
                )
            )
    return tuple(declarations)


def _input_sources(inputs: Mapping[str, object]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for key, value in inputs.items():
        if isinstance(value, Mapping) and value.get("binding_mode") == "WHOLE_ARTIFACT":
            source_task_id = value.get("source_task_id")
            output_id = value.get("output_id")
            sources[str(key)] = f"artifact.{source_task_id}:{output_id}"
        else:
            sources[str(key)] = f"blueprint.inputs.{key}"
    return sources


def build_execution_ir(
    plan: PlanningBlueprintPlan,
    *,
    workflow_family_id: str,
    workflow_version: int,
    snapshot: RegistrySnapshot,
) -> ExecutionIR:
    bindings = tuple(snapshot.bindings)
    nodes: list[IRNode] = []
    for task in plan.tasks:
        candidates = sorted(
            (item for item in bindings if item.capability in task.capabilities),
            key=lambda item: (item.requirement_id, item.agent_id, item.version),
        )
        if not candidates:
            raise CapabilityInvalid(
                f"no Registry Snapshot binding satisfies task capabilities: {task.task_id}"
            )
        binding = candidates[0]
        agent_binding = AgentBindingRef(
            registry_snapshot_id=snapshot.snapshot_id,
            requirement_id=binding.requirement_id,
            agent_id=binding.agent_id,
            version=binding.version,
            endpoint=binding.endpoint,
            capability=binding.capability,
            input_schema=binding.input_schema,
            output_schema=binding.output_schema,
            credential_ref=binding.credential_ref,
        )
        nodes.append(
            IRNode(
                node_id=task.task_id,
                name=task.objective,
                node_type="invoke_agent",
                depends_on=task.depends_on,
                input_contract=DataContract(
                    schema=binding.input_schema,
                    sources=_input_sources(task.inputs),
                ),
                output_contract=DataContract(
                    schema=binding.output_schema,
                    sources={str(key): f"artifact.{task.task_id}:{key}" for key in task.outputs},
                ),
                completion_criteria=task.completion_criteria,
                artifact_policy=ArtifactPolicy(
                    produces=tuple(f"{task.task_id}:{key}" for key in task.outputs),
                    consumes=tuple(
                        sorted(
                            source.removeprefix("artifact.")
                            for source in _input_sources(task.inputs).values()
                            if source.startswith("artifact.")
                        )
                    ),
                    registration_required=True,
                    reuse_allowed=False,
                ),
                agent_binding=agent_binding,
                retry_policy=_retry_policy(task.retry_policy),
            )
        )
    return ExecutionIR(
        ir_version="1.0",
        workflow_family_id=workflow_family_id,
        workflow_version=workflow_version,
        task_id=plan.plan_id,
        registry_snapshot_id=snapshot.snapshot_id,
        nodes=tuple(nodes),
        edges=tuple(IREdge(dep, task.task_id) for task in plan.tasks for dep in task.depends_on),
        completion_criteria=plan.completion_criteria,
        artifacts=_artifact_declarations(plan),
    )
