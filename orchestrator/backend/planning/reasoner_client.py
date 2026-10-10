from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Callable

from pydantic import ValidationError

from blueprint.normalizer import NormalizedBlueprint
from contracts.reasoner_proposal import ReasonerProposalV1


class ReasonerTransportError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class PlanSynthesisError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReasonerAudit:
    transport_attempt: int
    schema_repair_attempt: int
    operation_key: str
    request_hash: str
    response_hash: str | None
    validation_errors: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class ReasonerResult:
    proposal: ReasonerProposalV1
    audit: tuple[ReasonerAudit, ...]


Transport = Callable[[dict[str, object], str], dict[str, object]]


def _hash(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_capabilities(values: list[dict[str, object]]) -> list[dict[str, object]]:
    allowed = ("capability", "input_schema", "output_schema", "side_effect", "dry_run_supported", "usage_reporting")
    result: list[dict[str, object]] = []
    for item in values:
        result.append({key: item[key] for key in allowed if key in item})
    return result


def _safe_policy(value: dict[str, object]) -> dict[str, object]:
    allowed = ("allow_replan", "allow_rebinding", "side_effect_approval")
    return {key: value[key] for key in allowed if key in value}


def _validation_errors(error: ValidationError) -> tuple[dict[str, object], ...]:
    # Keep locations and error types, never echo the rejected response value.
    return tuple(
        {
            "path": ".".join(str(part) for part in item.get("loc", ())),
            "type": str(item.get("type", "validation_error")),
            "message": str(item.get("msg", "invalid response")),
        }
        for item in error.errors()
    )


class ReasonerClient:
    def __init__(
        self,
        transport: Transport,
        *,
        max_total_reasoner_calls: int = 3,
        max_transport_attempts: int = 3,
        max_schema_repair_attempts: int = 2,
        deadline_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_total_reasoner_calls < 1:
            raise ValueError("max_total_reasoner_calls must be positive")
        self.transport = transport
        self.max_total = max_total_reasoner_calls
        self.max_transport = max_transport_attempts
        self.max_repairs = max_schema_repair_attempts
        self.deadline_seconds = deadline_seconds
        self.clock = clock

    def synthesize(
        self,
        normalized: NormalizedBlueprint,
        *,
        capability_summaries: list[dict[str, object]],
        policy: dict[str, object],
    ) -> ReasonerResult:
        blueprint = normalized.blueprint.model_dump(mode="json")
        base_payload: dict[str, object] = {
            "schema_version": "reasoner-request.v1",
            "blueprint": blueprint,
            "capabilities": _safe_capabilities(capability_summaries),
            "policy": _safe_policy(policy),
            "identity": {
                "correlation_id": normalized.blueprint.correlation_id,
                "session_id": normalized.blueprint.session_id,
                "plan_version": normalized.blueprint.plan_version,
                "workflow_version": normalized.blueprint.workflow_version,
                "idempotency_key": normalized.blueprint.idempotency_key,
                "contract_version": normalized.blueprint.contract_version,
            },
        }
        operation_prefix = f"{normalized.blueprint.idempotency_key}:reasoner"
        started = self.clock()
        calls = 0
        transport_attempt = 0
        repairs = 0
        audit: list[ReasonerAudit] = []
        payload = base_payload

        while calls < self.max_total:
            if self.clock() - started >= self.deadline_seconds:
                raise PlanSynthesisError("PLAN_SYNTHESIS_FAILED: deadline exceeded before next call")
            transport_attempt += 1
            operation_key = f"{operation_prefix}:t{transport_attempt}:s{repairs}"
            request_hash = _hash(payload)
            calls += 1
            try:
                response = self.transport(payload, operation_key)
            except ReasonerTransportError as error:
                audit.append(ReasonerAudit(transport_attempt, repairs, operation_key, request_hash, None))
                if not error.retryable or transport_attempt >= self.max_transport:
                    raise PlanSynthesisError(f"PLAN_SYNTHESIS_FAILED: {error}") from error
                continue
            except Exception as error:
                audit.append(ReasonerAudit(transport_attempt, repairs, operation_key, request_hash, None))
                raise PlanSynthesisError(f"PLAN_SYNTHESIS_FAILED: transport error: {error}") from error

            response_hash = _hash(response)
            try:
                proposal = ReasonerProposalV1.model_validate(response)
            except ValidationError as error:
                errors = _validation_errors(error)
                audit.append(ReasonerAudit(transport_attempt, repairs, operation_key, request_hash, response_hash, errors))
                if repairs >= self.max_repairs or calls >= self.max_total:
                    raise PlanSynthesisError("PLAN_SYNTHESIS_FAILED: proposal schema invalid") from error
                repairs += 1
                payload = {
                    "schema_version": "reasoner-repair-request.v1",
                    "identity": base_payload["identity"],
                    "request_context": base_payload,
                    "previous_response_hash": response_hash,
                    "validation_errors": list(errors),
                }
                continue
            audit.append(ReasonerAudit(transport_attempt, repairs, operation_key, request_hash, response_hash))
            return ReasonerResult(proposal=proposal, audit=tuple(audit))

        raise PlanSynthesisError("PLAN_SYNTHESIS_FAILED: total reasoner call limit reached")
