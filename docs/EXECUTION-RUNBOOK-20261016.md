# Execution Runbook (2026-10-16)

For the most recent host-Agent, monitoring, Docker-network, and artifact-path
conditions, see [EXECUTION-CONDITIONS-20261023.md](EXECUTION-CONDITIONS-20261023.md).

This record captures the container and service set used for the successful Plan-to-workflow execution after the host restart.

## Verified execution

- Workflow: `orchv2f09f0c0da47dd91457`
- Workflow family: `wf-be6b70c0da3e51c00dbfec89`
- Workflow version: `1`
- n8n execution: `35` (`success`)
- Node order: `word_agent -> worker -> report_agent`
- Report: `/data/reports/test_report_allInOne.docx` in `module-dev-reportagent-reportagent-1`
- Plan source: `/home/test/codex/exec-plan.json`

## Trigger modes

The active workflow supports both entry modes with the same `word_agent -> worker -> report_agent` graph:

- **Webhook Plan mode**: `orchestrator::start` reads the POST request body and preserves the submitted Plan fields, including `test_path`.
- **Manual mode**: `orchestrator::manual-start` is usable from the n8n editor. When no webhook has run, it supplies the verified default test input:
  `/app/pytestproject/cases/test_TP82_test/test_TP82_test.py`, `timeout=300`, `generate_allure=true`, `use_xvfb=false`.
- The request expression selects the executed trigger lazily, so the manual path does not dereference the unexecuted webhook node and the webhook path does not replace the submitted Plan with manual defaults.

After changing the workflow in the database, n8n was restarted and the exported active workflow was checked for both the webhook body expression and the manual default path. Compiler regression tests passed (`9/9`).

## Artifact namespace update

Newly compiled V1/V2 workflows pass the following fields to every business
Agent: `plan_id`, `plan_version`, n8n `execution_id`, `artifact_namespace`,
`artifact_root`, and `artifact_path`. Their canonical prefix is:

```text
ai-agents/{agent}/{plan_id}_{plan_version}_{execution_id}/
```

The already-published execution `35` predates this compiler change; it is not
rewritten in place. Publish a new immutable PlanVersion/workflow version (or a
new Plan) before validating the new MinIO object keys.

## Required running containers

| Container | Role | Host port | Required network |
|---|---|---:|---|
| `pg-fresh` | n8n and execution PostgreSQL | internal `5432` | default agent network, `minio_n8n-net` |
| `redis-fresh` | n8n queue backend | internal `6379` | `minio_n8n-net` |
| `minio-fresh` | artifact storage | `9000`, `9001` | `minio_n8n-net` |
| `n8n-fresh` | n8n production workflow host | `5678` | default agent network, `minio_n8n-net` |
| `worker-fresh` | n8n queue worker (`EXECUTIONS_MODE=queue`) | internal `5678` | **default agent network**, `minio_n8n-net` |
| `module-dev-wordagent-wordagent-1` | `word_agent` | internal `4324` | default agent network |
| `module-dev-pytestproject-pytestproject-1` | `worker` | internal `5000` | default agent network |
| `module-dev-reportagent-reportagent-1` | `report_agent` | internal `6000` | default agent network |
| `orchestrator-gateway-dev` | workflow import/publish/start gateway | `127.0.0.1:27880` | default agent network, `minio_n8n-net` |
| `p009-execution-service` | execution orchestration API | `0.0.0.0:28020` | default agent network, `minio_n8n-net` |

The default agent network observed in this run was `agent-offline-bundle-20260625-portable-portable_default`. The exact name can change with the external agent bundle; discover it from the word agent container instead of hard-coding it.

## Startup order

From `/home/test/codex/p009-stage`:

```bash
./scripts/start-all.sh
```

For the currently installed external-agent containers, the equivalent recovery sequence is:

```bash
docker start pg-fresh redis-fresh minio-fresh
docker start module-dev-wordagent-wordagent-1 module-dev-pytestproject-pytestproject-1 module-dev-reportagent-reportagent-1
docker start n8n-fresh worker-fresh
docker start orchestrator-gateway-dev p009-execution-service

AGENT_NETWORK=$(docker inspect -f '{{range $name, $config := .NetworkSettings.Networks}}{{$name}} {{end}}' module-dev-wordagent-wordagent-1 | awk '{print $1}')
docker network connect "$AGENT_NETWORK" worker-fresh 2>/dev/null || true
```

The `start-all.sh` script now performs the worker network attachment automatically. This is required because the worker is managed by the external n8n stack and otherwise cannot resolve `wordagent`, `pytestproject`, or `reportagent`.

## Gateway and execution configuration

- Gateway: `http://127.0.0.1:27880`
- Execution API: `http://127.0.0.1:28020`
- n8n webhook host: `http://127.0.0.1:5678`
- Gateway API key: `/home/test/codex/p009-stage/.runtime-secrets/gateway-api-key`
- Webhook auth value: `/home/test/codex/p009-stage/.runtime-secrets/webhook-auth`
- Planning HMAC: `/home/test/codex/p009-stage/.runtime-secrets/planning-hmac`
- Execution submit token: `/home/test/codex/p009-stage/.runtime-secrets/execution-submit-token`

Do not copy secret contents into this runbook.

## Persistent project files

- Startup script: [`scripts/start-all.sh`](../scripts/start-all.sh)
- n8n compiler: [`orchestrator/backend/backends/n8n/workflow_compiler.py`](../orchestrator/backend/backends/n8n/workflow_compiler.py)
- Agent registry: `agent-registry/registration/agents.json`
- n8n PostgreSQL data: Docker volume `minio_pg_data`
- n8n workflow data: Docker volume `minio_n8n_data`
- Execution service data: Docker volume `p009-execution-data`

## Post-start checks

```bash
docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}'
docker exec worker-fresh node -e "require('dns').lookup('wordagent',(e,a)=>console.log(e||a))"
docker logs --tail 50 orchestrator-gateway-dev
docker logs --tail 50 n8n-fresh
```

Expected DNS output is an IP address for `wordagent`; an `EAI_AGAIN` result means the worker is not attached to the agent network.
