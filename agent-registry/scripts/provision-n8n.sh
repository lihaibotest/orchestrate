#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
COMPOSE=(docker compose --project-directory "$ROOT_DIR" -f "$ROOT_DIR/compose.yaml")
WORKFLOW_IDS=(
  agentRegistryRegister20260819
  agentRegistryQuery20260819
  agentRegistryHeartbeat20260819
  agentRegistryHealth20260819
  agentRegistryMaintenance20260819
)
CREDENTIAL_FILE="$(mktemp)"

cleanup() {
  rm -f "$CREDENTIAL_FILE"
}
trap cleanup EXIT

"${COMPOSE[@]}" up -d postgres
PROJECT_ID="$("${COMPOSE[@]}" exec -T postgres psql -U n8n -d n8n -Atc "SELECT id FROM project WHERE type='personal' ORDER BY id LIMIT 1")"
if [ -z "$PROJECT_ID" ]; then
  printf 'n8n personal project is unavailable\n' >&2
  exit 1
fi

cat >"$CREDENTIAL_FILE" <<'JSON'
[
  {
    "id": "agentRegistryPostgres20260819",
    "name": "Agent Registry PostgreSQL",
    "type": "postgres",
    "data": {
      "host": "postgres",
      "database": "agent_registry",
      "user": "agent_registry",
      "password": "agent_registry_dev_registry",
      "port": 5432,
      "ssl": "disable",
      "maxConnections": 10
    }
  }
]
JSON

"${COMPOSE[@]}" stop n8n >/dev/null 2>&1 || true
"${COMPOSE[@]}" run --rm --no-deps -v "$CREDENTIAL_FILE:/tmp/agent-registry-credential.json:ro" n8n import:credentials --input=/tmp/agent-registry-credential.json --projectId="$PROJECT_ID"
"${COMPOSE[@]}" run --rm --no-deps n8n import:workflow --separate --input=/bootstrap/workflows --projectId="$PROJECT_ID"
for workflow_id in "${WORKFLOW_IDS[@]}"; do
  "${COMPOSE[@]}" run --rm --no-deps n8n publish:workflow --id="$workflow_id"
done
"${COMPOSE[@]}" up -d n8n
