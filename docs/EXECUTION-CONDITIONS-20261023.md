# Workflow Execution Conditions (2026-10-23)

## Latest Validated Snapshot

The latest end-to-end run completed successfully after the MinIO object-key
and Word timeout fixes:

- ExecPlan: `pln_01a16883-e256-7255-bf14-599d55e00647:v1`
- Blueprint: `blueprint-6ff07844e0e0864b69609a79c5826416`
- Workflow: `execution-orchestrator-wf-6cd4eb49bedb36806dffba1e-v1`
- n8n execution: `88` (`success`)
- Monitoring run: `project_id=smart-test-platform`, `task_id=pln_01a16883-e256-7255-bf14-599d55e00647`, `run_id=1`
- Monitoring result: `completed`; Word, Worker, Report, and workflow root all `end`

The latest MinIO object keys are bucket-relative. The bucket is `ai-agents`;
the object key must start directly with the Agent name:

```text
ai-agents/word_agent/{plan_id}_{plan_version}_{n8n_execution_id}/
ai-agents/worker/{plan_id}_{plan_version}_{n8n_execution_id}/
ai-agents/report_agent/{plan_id}_{plan_version}_{n8n_execution_id}/
```

Do not create `ai-agents/ai-agents/...`. The three Agent storage clients strip
one bucket-qualified `ai-agents/` prefix at the MinIO boundary. Existing old
objects were cleared on 2026-10-23 before this validation.

The Word Agent HTTP node in newly compiled workflows has a `120000ms` timeout
floor. This prevents the normal document-generation latency from triggering
n8n's 30-second retry and producing duplicate documents. Published immutable
workflow versions retain their original timeout.

This snapshot records the working conditions used for the external-Agent
workflow validation. It contains no secrets and does not require modifying the
monitoring project.

## Earlier Validation Reference

- Plan: `pln_01a1629d-54c5-7083-8994-e13cff50837d`
- Plan version / monitoring run: `1`
- Workflow family / version: `wf-f0b5ef6731e5f5bc391423f5` / `1`
- n8n workflow webhook path:
  `execution-orchestrator/7757507f2dce7817e43096a92889692c/start`
- Request payload reference: `/tmp/p009-payload.json`.
  For a Word artifact-producing run, it includes both `item_filepath` and
  `case_filepath`, with `filter_items: false`.

## Source Paths

| Component | Host path | Host port |
| --- | --- | ---: |
| Execution/orchestrator project | `/home/test/codex/p009-stage` | n/a |
| Word Agent | `/home/test/MinIO（复件）/test_word_gener_update_v5.0` | `4324` |
| Worker Agent | `/home/test/MinIO（复件）/worker-1` | `5000` |
| Report Agent | `/home/test/MinIO（复件）/report_agent` | `6000` |
| Real monitoring project | `/home/test/AgentEvalTeam/AgentEvalTeam` | `8080` |

The three host Agent services are started with the local Python environments
under `/home/test/anaconda3/envs/` and bind to `0.0.0.0` on the listed ports.
The real monitoring process is the Django command:

```bash
/home/test/anaconda3/envs/agentevalteam/bin/python manage.py runserver 0.0.0.0:8080
```

Run it from `/home/test/AgentEvalTeam/AgentEvalTeam`. The monitoring service is
not a Docker container. Do not modify its source code for this workflow.

## Required Containers

The following containers were running when this snapshot was recorded:

| Container | Purpose | Published port |
| --- | --- | --- |
| `pg-fresh` | n8n PostgreSQL | internal `5432` |
| `redis-fresh` | n8n queue backend | internal `6379` |
| `minio-fresh` | MinIO object storage | `9000`, `9001` |
| `n8n-fresh` | n8n main process | `5678` |
| `worker-fresh` | n8n queue worker | internal `5678` |
| `orchestrator-gateway-dev` | workflow import/publish gateway | `127.0.0.1:27880` |
| `p009-execution-service` | execution API | `28020` |
| `module-dev-wordagent-wordagent-1` | legacy/container Word Agent | internal `4324` |
| `module-dev-pytestproject-pytestproject-1` | legacy/container Worker | internal `5000` |
| `module-dev-reportagent-reportagent-1` | legacy/container Report Agent | internal `6000` |

For this validation, the compiled workflow must call the host Agent services,
not the legacy Agent containers:

```text
Word    http://172.26.0.1:4324
Worker  http://172.26.0.1:5000
Report  http://172.26.0.1:6000
```

## Docker Networks and Connectivity

`n8n-fresh` and `worker-fresh` must both be attached to:

```text
agent-offline-bundle-20260625-portable-portable_default
minio_n8n-net
```

`minio-fresh` is attached to `minio_n8n-net`. The first network is needed for
the established n8n Agent runtime, and the second makes MinIO available to the
n8n stack. Confirm attachment after a restart:

```bash
docker inspect -f '{{.Name}} {{range $name, $config := .NetworkSettings.Networks}}{{$name}} {{end}}' \
  n8n-fresh worker-fresh minio-fresh
```

The host gateway reachable from n8n is `172.26.0.1`. The Docker subnet must be
allowed to access TCP `8080` on the host. The compiled workflow uses the
following monitoring endpoint:

```text
http://172.26.0.1:8080
```

It is the default `monitoring_endpoint` in
`orchestrator/backend/infrastructure/config.py`; do not replace it with the
host's LAN address. From n8n, the monitoring calls are:

```text
POST /api/monitoring/agent-tree/
POST /api/monitoring/agent-status/
```

The status flow is `word_agent running/end`, `worker running/end`,
`report_agent running/end`, then the workflow root `end`.

## Workflow and Artifact Contract

- Input/output order is strictly `word_agent -> worker -> report_agent`.
- Worker consumes the Word response's `artifact_root`/`minio_urls`.
- Report Agent consumes the Worker response's MinIO artifacts.
- Each agent writes newly created objects below:

```text
ai-agents/{agent}/{plan_id}_{plan_version}_{n8n_execution_id}/
```

Here `ai-agents` at the beginning denotes the bucket name. In MinIO object-key
queries, use `{agent}/{plan_id}_{plan_version}_{n8n_execution_id}/` as the
prefix; a URI may display the bucket followed by that key.

- The execution ID is the actual n8n execution ID, not the monitoring run ID.
- Object validation must check all three agent prefixes for the newly created
  execution. Prior successful execution `83` had Worker and Report artifacts;
  its Word call returned no artifact because the Word input was filtered to an
  empty item set. Use `filter_items: false` for the rerun that validates all
  three prefixes.

## Recovery and Verification

1. Start `pg-fresh`, `redis-fresh`, `minio-fresh`, `n8n-fresh`, and
   `worker-fresh`, then ensure the two required n8n/worker network attachments.
2. Start the three host Agents in one command, from any working directory:

   ```bash
   /home/test/codex/p009-stage/scripts/start-external-agents.sh
   ```

   The script starts only missing services and writes to the established Agent
   log paths. Use `start-external-agents.sh status` for a read-only port/PID
   check. Start the real monitoring service separately from the path above;
   confirm listeners on `4324`, `5000`, `6000`, and `8080`.
3. Confirm gateway and execution service listeners on `27880` and `28020`, and
   n8n on `5678`.
4. For a reused monitoring `run_id`, reset only that run to `running`, clear
   old dynamic node timestamps, and re-register the already supplied
   `plan_tree`. Do not send a new tree with different topology.
5. Trigger the authenticated n8n webhook using the value stored in
   `.runtime-secrets/webhook-auth`; do not print or commit the value.
6. Query the real monitoring current-run endpoint with `project_id`, `task_id`,
   and `run_id=1`; it must finish with every Agent and the root at `end`.
7. Inspect MinIO for all three canonical prefixes using the new n8n execution
   ID.

## Host Agent Services

The current host services are managed as user-level transient units:

```text
p009-word-agent.service
p009-worker.service
p009-report-agent.service
```

Their active commands and paths are:

```text
/home/test/anaconda3/envs/word_gen/bin/python api_service.py
  cwd=/home/test/MinIO（复件）/test_word_gener_update_v5.0
  port=4324

/home/test/anaconda3/envs/pytest/bin/python pytest_api.py
  cwd=/home/test/MinIO（复件）/worker-1
  port=5000

/home/test/anaconda3/envs/report/bin/python web_app.py
  cwd=/home/test/MinIO（复件）/report_agent
  port=6000
```

Word Agent MinIO and logging configuration is in its `.env`:

```text
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_SECURE=false
MINIO_BUCKET=ai-agents
WORD_AGENT_LOG_DIR=/home/test/MinIO（复件）/logs/word_agent
```

The Word, Worker, and Report logs are respectively under
`/home/test/MinIO（复件）/logs/word_agent/`,
`/home/test/MinIO（复件）/logs/worker/`, and
`/home/test/MinIO（复件）/logs/report_agent/`.

## Network Persistence

The real monitoring service is a host process on `0.0.0.0:8080`. UFW is
enabled at boot (`/etc/ufw/ufw.conf: ENABLED=yes`) and persistently allows TCP
8080 from `172.26.0.0/16`; the rule is also loaded in the active
`ufw-user-input` chain. The n8n bridge is:

```text
agent-offline-bundle-20260625-portable-portable_default
subnet=172.26.0.0/16 gateway=172.26.0.1
n8n-fresh=172.26.0.8
```

The compiled monitoring endpoint remains `http://172.26.0.1:8080` and must not
be changed to the host LAN address.

Useful no-secret checks:

```bash
docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}'
ss -ltnp | rg ':8080|:4324|:5000|:6000|:5678|:9000|:9001|:27880|:28020'
curl -fsS 'http://127.0.0.1:8080/api/monitoring/current/?project_id=smart-test-platform&task_id=pln_01a1629d-54c5-7083-8994-e13cff50837d&run_id=1'
```
