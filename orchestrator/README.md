# Standalone Orchestrator Agent

For offline relocation, use [docs/MIGRATION.md](docs/MIGRATION.md) and the single `scripts/start-all.sh` launcher. This development bundle is unsigned and provides SHA256 integrity checks only.

This package connects to external n8n, a planning agent, and a configured Workflow LLM. An evaluator is optional: when a healthy evaluator is present in the immutable Agent Registry, its endpoint and credential reference are compiled into direct per-node and final evaluator POSTs. Evaluator opinions travel from the evaluator to the Planning Agent through the evaluator-owned direct channel; they never pass through Orchestrator. `_source-reference/` is staging evidence and must never enter the release archive.

The development bundle supports DeepSeek as the v2 Workflow Reasoner through the
OpenAI-compatible Chat Completions endpoint. Set
`ORCHESTRATOR_WORKFLOW_LLM_BASE_URL=https://api.deepseek.com` and
`ORCHESTRATOR_WORKFLOW_LLM_MODEL=deepseek-v4-flash`. The adapter asks for JSON-only
`reasoner-proposal.v1` output and the existing `ReasonerClient` applies strict Pydantic
schema validation before any Registry resolution, IR build, compilation, or publication.
Model output cannot supply Agent endpoints, credential references, executor IDs, n8n JSON,
code, or secrets.

Version 1.1.0 is an **unsigned development bundle** intended for direct offline migration between development machines. `SHA256SUMS` and the archive `.sha256` detect corruption; they do not establish publisher identity. The bundle intentionally contains no artifact private key, public key, signature, key ID, or trust anchor. Runtime HMAC authentication for Planning Agent and n8n gateway calls remains mandatory and uses mounted secret files. The evaluator owns its separate Planning Agent HMAC configuration.

The runtime uses one Uvicorn worker as the single SQLite writer. Request concurrency is handled inside that process; SQLite enables foreign keys, WAL, and a 5000 ms busy timeout. Do not scale this Compose service to multiple writer containers against one volume. Incomplete event leases and pending outbox records are recovered when the process starts.

SQLite upgrades are ordered through schema versions 1 through 10. Before each pending
migration, the SQLite backup API creates a consistent `.pre-migration-vN.bak`; every
backup and upgraded database must pass `PRAGMA integrity_check`.

For an offline source rebuild, first load the base image whose content ID is recorded
in `artifacts/image-manifest.json`, then run `./scripts/build-image-offline.sh`. The
script rejects any missing or different base image and builds with networking disabled.

Failure handling uses only Orchestrator-owned runtime facts. `max_attempts` is inclusive:
`4` means one initial execution plus three technical retries. Timeout, network, HTTP 429,
and HTTP 5xx are retried on the same workflow version, input, and idempotency key. Binding,
schema, dependency, and checkpoint failures enter bounded self-repair. The default maximum
is two repair attempts per session and workflow lineage; every repaired version must pass
validation and Canary. Confirmed capability, backend, dependency, budget, or exhausted-repair
boundaries create a separate `workflow-escalation.v1` outbox operation for the Planning Agent.

Published workflow versions are immutable. A new version is created inactive, validated before promotion, and never PUT-overwrites an existing version. Retirement is allowed only when n8n reports that no running execution or Wait checkpoint references the version.

After a workflow is created and activated, the runtime automatically POSTs the orchestration
payload to the workflow's authenticated, keyed webhook trigger. Callers do not need to click Execute in
n8n. The generated workflow includes this trigger and connected node graph; the blueprint response
includes n8n's trigger response in `trigger_result`. A workflow without a webhook trigger is
rejected by the automatic-start adapter.

## v2 execution path

Set `ORCHESTRATOR_V2_ENABLED=true` only when the external n8n gateway reports the complete
`orchestrator-gateway.v1` capability contract. `ORCHESTRATOR_V2_AGENT_ALLOWED_HOSTS` is a
comma-separated exact host allowlist; private Agent networks must also be explicitly listed
in `ORCHESTRATOR_V2_ALLOWED_PRIVATE_CIDRS`. The v2 path queues a durable planning job, then
normalizes and preflights the Blueprint, obtains a constrained Reasoner proposal, resolves
Agents from an immutable Registry snapshot, builds and verifies Execution IR, compiles n8n
JSON, creates it inactive, validates/Canaries/activates it, and starts it automatically.

Runtime Agent calls must first obtain a budget reservation from the gateway. Runtime events
are checked against immutable backend execution identity, deduplicated, adapted to canonical
runtime facts, and converted to a finite decision (`CONTINUE`, `RETRY`, `PATCH`, `REPLAN`,
`FAIL`, or `ABORT`). Technical retry keeps the same workflow version and idempotency key.
`PATCH` invokes Orchestrator-owned workflow repair; `REPLAN` is reserved for a proven external
or plan boundary. A new version advances only after a materially changed IR passes validation,
Canary when required, durable publication, and start reconciliation.

## Offline migration

1. Copy `orchestrator-agent-standalone-dev-1.1.0.tar.gz` and its `.sha256` file.
2. Run `sha256sum -c orchestrator-agent-standalone-dev-1.1.0.tar.gz.sha256`, then extract it.
3. Create the six required files referenced by `.env.example`, then set the local
   development permissions:

   ```bash
   mkdir -p .runtime-secrets
   chmod 700 .runtime-secrets
   printf '%s\n' 'your DeepSeek API key' > .runtime-secrets/deepseek-api-key
   chmod 644 .runtime-secrets/*
   ```

   Compose file-secret bind mounts preserve the source file permission bits on
   this platform. The files therefore need the read bit for container UID
   10001. The `0700` parent directory still prevents other host users from
   traversing the directory. Put the DeepSeek API key only in
   `.runtime-secrets/deepseek-api-key`; never write it into `.env`, `.env.example`, image,
   source map, or archive. Do not place this directory inside the archive.
4. Configure the Orchestrator-owned external URLs, credential references, v2 host allowlist, port, and secret file paths in `.env`. Evaluator endpoint selection comes only from the immutable Registry snapshot; evaluator-to-Planning-Agent configuration belongs to the evaluator deployment.
5. Run `./scripts/start.sh`. It verifies hashes and image layers before `docker load` and starts a new Compose project without building.
6. Stop only this package with `./scripts/stop.sh`.

The real n8n gateway contract must be tested in the target environment. A development archive
can be generated when that external service is absent, but test-double evidence is not a claim
that the real automatic execution chain has been verified.

The package expects the n8n credential referenced by `N8N_WEBHOOK_CREDENTIAL_REF` to
validate the `N8N_WEBHOOK_AUTH_HEADER` value. HMAC secrets and webhook values are read
only from mounted Docker secret files.

## Evaluator separation and monitoring fallback

Before n8n starts an execution, the compiler reads the immutable Registry snapshot for that workflow version. A candidate enables direct evaluator nodes only when it has `role=evaluator`, `health=healthy`, and capability `evaluate-workflow`. The compiled graph retains only that endpoint and credential reference; Orchestrator has no evaluator base URL or evaluator callback secret.

When enabled, evaluator nodes POST directly to that candidate and terminal paths converge on one direct final evaluator POST. The evaluator independently sends `evaluator-planning-agent.v1` to the Planning Agent. Orchestrator never receives, stores, classifies, repairs from, or forwards evaluator opinions, scores, verdicts, recommendations, or raw payloads.

When the immutable snapshot has no usable evaluator binding, or the compiled graph has zero compiler-marked monitoring nodes, the lineage persists `planner_final_summary_fallback`. Its terminal runtime callback creates exactly one `workflow-final-summary.v1` outbox operation containing bounded Orchestrator-owned execution facts and references. workflow-final-summary.v1 is not an evaluator relay. A failed direct evaluator HTTP request is an ordinary runtime failure and never switches a persisted `evaluator_direct` lineage to fallback.
