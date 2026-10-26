from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from backends.n8n.compiler_v2 import N8nV2Compiler
from blueprint.normalizer import normalize_blueprint
from blueprint.preflight import run_preflight
from capabilities.models import CapabilityRegistryV2, CapabilityRequirement
from capabilities.ranking import RankingWeights
from capabilities.registry import EndpointPolicy, RegistrySnapshot, build_registry_snapshot
from capabilities.resolver import BindingDecision, resolve_capability
from contracts.orchestration import WorkflowEscalationV1
from persistence.execution_plan_repository import ExecutionPlanRepository
from persistence.runtime_repository import RuntimeRepository
from planning.execution_planner import build_execution_ir
from planning.reasoner_client import ReasonerClient
from services.planning_agent_delivery import ESCALATION_OUTBOX_KIND


class PlanningJobRunner:
    def __init__(
        self,
        database_path: Path,
        *,
        registry_loader: Callable[[], dict[str, object]],
        endpoint_policy: EndpointPolicy,
        reasoner: ReasonerClient,
        publisher: object,
        compiler_context: dict[str, object],
        ranking_weights: RankingWeights | None = None,
    ) -> None:
        self.plans = ExecutionPlanRepository(database_path)
        self.runtime = RuntimeRepository(database_path)
        self.registry_loader = registry_loader
        self.endpoint_policy = endpoint_policy
        self.reasoner = reasoner
        self.publisher = publisher
        self.ranking_weights = ranking_weights or RankingWeights()
        self.compiler_context = dict(compiler_context)
        self.compiler = N8nV2Compiler()

    def _binding(self, snapshot: RegistrySnapshot, task: object) -> BindingDecision:
        candidates = [
            candidate for candidate in snapshot.candidates
            if candidate.role == "business"
            and candidate.side_effect == task.side_effect
            and any(capability in candidate.capabilities for capability in task.capabilities)
        ]
        if not candidates:
            raise ValueError(f"no candidate schema for task {task.task_id}")
        template = candidates[0]
        requirement = CapabilityRequirement(
            capability=next(item for item in task.capabilities if item in template.capabilities),
            input_schema=template.input_schema,
            output_schema=template.output_schema,
            security_scopes=template.security_scopes,
            backend="n8n",
            side_effect=task.side_effect,
            canary=False,
            require_usage_reporting=True,
        )
        return resolve_capability(snapshot, requirement, weights=self.ranking_weights)

    def run(self, job_id: str) -> dict[str, object]:
        current = self.plans.get_planning_job(job_id)
        if current is None:
            raise KeyError(job_id)
        if current["status"] == "started":
            return dict(current["result"])
        self.plans.mark_job_running(job_id)
        try:
            normalized = normalize_blueprint(self.plans.load_job_blueprint(job_id))
            snapshot = build_registry_snapshot(
                CapabilityRegistryV2.model_validate(self.registry_loader()),
                endpoint_policy=self.endpoint_policy,
            )
            evaluation_enabled = any(
                candidate.role == "evaluator"
                and candidate.health == "healthy"
                and "evaluate-workflow" in candidate.capabilities
                for candidate in snapshot.candidates
            )
            evaluator_binding: dict[str, object] | None = None
            if evaluation_enabled:
                evaluators = [
                    candidate for candidate in snapshot.candidates
                    if candidate.role == "evaluator"
                    and candidate.health == "healthy"
                    and "evaluate-workflow" in candidate.capabilities
                ]
                if len(evaluators) != 1:
                    raise ValueError("registry must contain exactly one healthy evaluator binding")
                evaluator = evaluators[0]
                evaluator_binding = {
                    "agent_id": evaluator.agent_id,
                    "endpoint": evaluator.endpoint,
                    "credential_ref": evaluator.n8n_credential_ref,
                    "snapshot_hash": snapshot.snapshot_hash,
                }
            preflight = run_preflight(
                normalized,
                snapshot,
                backend_features=self.compiler.capabilities(),
                evaluation_enabled=evaluation_enabled,
            )
            if not preflight.ready:
                required_decisions = {
                    "CAPABILITY_UNAVAILABLE": "provide a compatible healthy capability or revise the plan",
                    "EVALUATOR_METRIC_UNAVAILABLE": "provide evaluator metric support or revise completion criteria",
                    "BACKEND_FEATURE_UNAVAILABLE": "provide backend feature support or revise the plan",
                }
                for issue in sorted(preflight.issues, key=lambda item: (item.code, item.path)):
                    issue_key = hashlib.sha256(
                        f"{issue.code}:{issue.path}".encode("utf-8")
                    ).hexdigest()[:16]
                    operation_key = (
                        f"escalation:{normalized.blueprint.session_id}:preflight:"
                        f"{normalized.blueprint.plan_version}:{issue.code}:{issue_key}"
                    )
                    escalation = WorkflowEscalationV1(
                        schema_version="workflow-escalation.v1",
                        contract_version="workflow-escalation.v1",
                        caller_identity="orchestrator-escalation",
                        correlation_id=normalized.blueprint.correlation_id,
                        session_id=normalized.blueprint.session_id,
                        plan_version=normalized.blueprint.plan_version,
                        workflow_version=1,
                        reason_code=issue.code,
                        required_decision=required_decisions.get(
                            issue.code, "revise the plan using the preflight evidence"
                        ),
                        missing_capabilities=(
                            [issue.path]
                            if issue.code
                            in {"CAPABILITY_UNAVAILABLE", "EVALUATOR_METRIC_UNAVAILABLE"}
                            else []
                        ),
                        repair_attempts=0,
                        evidence=[
                            {
                                "ref": f"preflight://{issue.path}",
                                "error_code": issue.code,
                                "detail": issue.message,
                            }
                        ],
                        operation_key=operation_key,
                    ).model_dump(mode="json")
                    self.plans.record_outbox(
                        outbox_id=operation_key,
                        session_id=normalized.blueprint.session_id,
                        kind=ESCALATION_OUTBOX_KIND,
                        payload=escalation,
                        operation_key=operation_key,
                    )
                raise ValueError(f"PREFLIGHT_FAILED: {[issue.code for issue in preflight.issues]}")
            summaries = [
                {
                    "capability": capability,
                    "input_schema": candidate.input_schema,
                    "output_schema": candidate.output_schema,
                    "side_effect": candidate.side_effect,
                    "dry_run_supported": candidate.dry_run_supported,
                    "usage_reporting": candidate.usage_reporting,
                }
                for candidate in snapshot.candidates if candidate.role == "business"
                for capability in candidate.capabilities
            ]
            reasoned = self.reasoner.synthesize(
                normalized,
                capability_summaries=summaries,
                policy=normalized.blueprint.execution_policy.model_dump(mode="json"),
            )
            bindings = {
                task.task_id: self._binding(snapshot, task)
                for task in normalized.blueprint.tasks
            }
            built = build_execution_ir(
                normalized,
                snapshot,
                reasoned.proposal,
                bindings,
                evaluation_enabled=evaluation_enabled,
            )
            compiler_bindings = {
                f"binding-{task_id}": {
                    "endpoint": decision.selected.endpoint,
                    "credential_ref": decision.selected.n8n_credential_ref,
                    "agent_id": decision.selected.agent_id,
                }
                for task_id, decision in bindings.items()
            }
            artifact = self.compiler.compile(
                built.ir,
                {
                    **self.compiler_context,
                    # Keep the upstream Plan identity separate from the
                    # n8n execution identity.  The former is stable across
                    # replays; the latter is appended at run time to form
                    # the per-workflow Agent artifact namespace.
                    "plan_id": normalized.blueprint.plan_id,
                    "bindings": compiler_bindings,
                    "evaluation_enabled": evaluation_enabled,
                    "evaluator_binding": evaluator_binding,
                },
            )
            artifact_metadata = artifact.get("metadata")
            if not isinstance(artifact_metadata, dict):
                raise ValueError("compiler artifact lacks monitoring metadata")
            monitoring_mode = artifact_metadata.get("monitoring_mode")
            monitoring_node_count = artifact_metadata.get("monitoring_node_count")
            if not isinstance(monitoring_node_count, int) or isinstance(
                monitoring_node_count, bool
            ):
                raise ValueError("compiler monitoring node count must be an integer")
            if monitoring_mode == "evaluator_direct":
                if not evaluation_enabled or monitoring_node_count < 1:
                    raise ValueError("compiler evaluator-direct monitoring metadata is inconsistent")
            elif monitoring_mode == "planner_final_summary_fallback":
                if monitoring_node_count != 0:
                    raise ValueError("compiler fallback monitoring metadata is inconsistent")
            else:
                raise ValueError("compiler artifact has invalid monitoring mode")
            published = self.publisher.publish(
                job_id=job_id,
                session_id=normalized.blueprint.session_id,
                plan_version=normalized.blueprint.plan_version,
                workflow_version=1,
                artifact=artifact,
                start_payload={
                    "plan_id": normalized.blueprint.plan_id,
                    "idempotency_key": normalized.blueprint.idempotency_key,
                    "query": "登录" if "登录" in normalized.blueprint.goal.objective else normalized.blueprint.goal.objective,
                    "user_intent": normalized.blueprint.goal.objective,
                },
                canary=built.ir.canary_policy == "required",
            )
            result = {
                "status": "started",
                "plan_id": normalized.blueprint.plan_id,
                "ir_hash": built.ir.metadata.ir_hash,
                "workflow_id": published["workflow_id"],
                "execution_id": published["execution_id"],
            }
            proposal_hash = hashlib.sha256(
                json.dumps(reasoned.proposal.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            safe_bindings = {
                task_id: {
                    "agent_id": decision.selected.agent_id,
                    "rejected": [
                        {"agent_id": item.agent_id, "reasons": list(item.reasons)} for item in decision.rejected
                    ],
                }
                for task_id, decision in bindings.items()
            }
            self.plans.complete_job(
                job_id=job_id,
                plan_id=str(result["plan_id"]),
                session_id=normalized.blueprint.session_id,
                plan_version=normalized.blueprint.plan_version,
                workflow_version=1,
                blueprint_hash=normalized.blueprint_hash,
                snapshot_hash=snapshot.snapshot_hash,
                proposal_hash=proposal_hash,
                ir_hash=str(built.ir.metadata.ir_hash),
                ir=built.ir.model_dump(mode="json"),
                bindings=safe_bindings,
                validation_report={"ok": built.report.ok, "errors": []},
                result=result,
                workflow_spec=normalized.blueprint.model_dump(mode="json"),
                compiled_artifact=artifact,
                registry_snapshot=snapshot.model_dump(mode="json"),
                binding_metadata={
                    **compiler_bindings,
                    **({"evaluator": evaluator_binding} if evaluator_binding is not None else {}),
                },
                monitoring_mode=monitoring_mode,
                monitoring_node_count=monitoring_node_count,
                final_summary_operation_key=(
                    f"final-summary:{normalized.blueprint.session_id}:{published['execution_id']}"
                    if monitoring_mode == "planner_final_summary_fallback"
                    else None
                ),
            )
            self.runtime.create_backend_execution(
                execution_id=str(published["execution_id"]),
                session_id=normalized.blueprint.session_id,
                plan_version=normalized.blueprint.plan_version,
                workflow_version=1,
                backend="n8n",
                backend_workflow_id=str(published["workflow_id"]),
                backend_execution_id=str(published["execution_id"]),
            )
            constraints = normalized.blueprint.constraints
            self.runtime.create_budget(
                str(published["execution_id"]),
                max_calls=constraints.max_agent_calls,
                max_cost_microusd=constraints.max_cost_microusd,
                max_duration_seconds=constraints.max_duration_seconds,
            )
            return result
        except Exception as error:
            self.plans.fail_job(job_id, "PLANNING_JOB_FAILED", str(error))
            raise
