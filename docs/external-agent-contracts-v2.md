# External Agent Contracts v2

This document fixes the inter-agent boundaries for standalone Orchestrator 1.1.0. The JSON files under `schemas/v2/` are normative. The implementation supports a deliberately small offline JSON Schema subset: object and scalar types, arrays, required properties, closed objects, bounds, enums, constants, references, and `oneOf`; unknown JSON Schema keywords are rejected rather than silently ignored.

## Shared Identity

Every state-changing or state-reporting v2 message carries `correlation_id`, `session_id`, `plan_version`, `workflow_version`, and `idempotency_key`. Initial blueprints use workflow version zero. Published executions use immutable workflow and backend execution identities.

## Boundary Owners

- The planning agent sends Planning Blueprint v2, receives evaluator opinions through the evaluator-owned direct channel, receives Orchestrator escalation/fallback facts through two separate Orchestrator-owned channels, and may send explicit external directives. It never selects endpoints or credentials.
- The Workflow Reasoner returns only schema-constrained grouping, binding, control-structure, and evaluation-gate proposals. It cannot return executor identity, endpoint, credential material, backend JSON, or executable code.
- The Registry/gateway supplies an immutable candidate snapshot, complete input/output schemas, endpoint and credential references, and deterministic gateway operations.
- The evaluator is the sole producer of declared quality metrics and structured failure/quality outcomes. Each result has one stable evaluation ID.
- Orchestrator never receives evaluator opinions. It consumes only n8n runtime facts, performs bounded workflow-local repair, and emits a Planning Agent escalation only for a proven external or plan boundary.
- A business agent honors the stable idempotency key, declares side-effect behavior, and reports calls, duration, and integer cost usage when cost enforcement is required.
- The n8n gateway authenticates workflow credential identity against workflow/session/node bindings, forwards HMAC-authenticated events, and executes idempotent backend commands with operation lookup.

## Sensitive Data Order

At every boundary the order is: allowlist projection, size limits, recursive redaction, canonical hashing, and only then transport. This happens before persistence, logging, or LLM transport. Raw request and response payloads are not retained by default. Persistence stores the canonical hash plus schema-allowed, recursively redacted structured fields.

Keys matching authorization, API key, bearer, cookie, credential, password, token, private key, secret, or webhook authentication patterns are replaced regardless of nesting depth. Oversized strings, stderr, metrics maps, arrays, and object depth are rejected or deterministically truncated according to the versioned sensitive-data policy; the audit record states which action occurred.

## Runtime Authentication

Development-bundle signing is disabled, but runtime authentication is not. Planning agent, n8n gateway, and evaluator calls continue to use mounted-secret HMAC, timestamp freshness, nonce replay prevention, caller permissions, and immutable session/version checks. Shared secret values never appear in a Blueprint, Reasoner proposal, WorkflowSpec, Execution IR, n8n artifact, database audit response, or development archive.

## Evaluator to Planning Agent Direct Channel

The evaluator configures its own Planning Agent address with `EVALUATOR_PLANNING_AGENT_BASE_URL` and posts `evaluator-planning-agent.v1` to `/api/v2/evaluator-results`. The request uses caller identity `evaluator` and HMAC-SHA256 over the caller, timestamp, nonce, correlation ID, and canonical JSON body. The secret is loaded by the evaluator from its own mounted secret reference; it is never configured in or exposed to Orchestrator.

The closed payload carries `contract_version`, `correlation_id`, `session_id`, `plan_version`, `workflow_version`, `evaluation_id`, and `idempotency_key` with the evaluator-owned quality metrics and opinion. The Planning Agent validates the caller identity and signature, rejects stale or replayed requests, and suppresses duplicate logical deliveries by `idempotency_key`.

The fixed delivery policy makes three attempts for timeout, network, HTTP 429, and HTTP 5xx responses. Each attempt has a timeout of 10 seconds with bounded backoff. The evaluator owns failure persistence and retry recovery, including recovery after evaluator restart. A terminal rejection remains in the evaluator-owned dead-letter state for operator inspection. Orchestrator cannot observe or forward this message, and the protocol never uses `relay_evaluation()` or an Orchestrator endpoint.

## Orchestrator Runtime Facts and Self-Repair

`RuntimeEventV2` is the public v2 telemetry contract. Legacy `N8NEvent` is adapted to the same internal `RuntimeTelemetryV1`; that internal model is not a new endpoint. Both inputs recursively reject evaluator identity, evaluation result, quality score/verdict, opinion, recommendation, and verdict fields. The legal runtime action name `REQUEST_EVALUATION` remains a historical action token only and does not authorize evaluator data in telemetry.

The deterministic classifier returns `workflow_local`, `external_boundary`, or `uncertain` with one of `retry_same_version`, `repair_new_version`, `escalate`, or `stop`. Self-repair is capped at two attempts per `session_id` and workflow lineage and remains persisted across restart. Repaired workflows resume once from their durable checkpoint with a derived stable idempotency key, require Canary, and preserve the parent/repaired execution relationship.

## Orchestrator to Planning Agent Channels

Confirmed plan or external boundaries use `send_workflow_escalation()` and POST `workflow-escalation.v1` to `/api/v2/workflow-escalations` with caller identity `orchestrator-escalation`. Monitoring-absent terminal summaries use `send_workflow_final_summary()` and POST `workflow-final-summary.v1` to `/api/v2/workflow-final-summaries` with caller identity `orchestrator-final-summary`. Both use HMAC-SHA256, a correlation ID, a unique operation key, a 10-second timeout, five attempts for timeout/network/HTTP 429/HTTP 5xx, durable outbox failure state, restart recovery, and operation-key duplicate suppression.

The two outbox kinds are `planning.workflow_escalation` and `planning.workflow_final_summary`. Neither channel uses an evaluator route or `relay_evaluation()`. Final summary fallback is selected only from the immutable Registry snapshot plus compiler-marked monitoring-node count; evaluator HTTP success or failure cannot change the persisted mode. The final summary contains bounded execution states, output/artifact/log/checkpoint references, input summary, and retry/repair history. It contains no evaluator identity, payload, score, opinion, verdict, recommendation, or repair directive.
