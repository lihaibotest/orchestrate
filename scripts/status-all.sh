#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${MIGRATION_ENV_FILE:-$ROOT_DIR/.env}"
docker compose --project-directory "$ROOT_DIR" -f "$ROOT_DIR/docker-compose.migration.yml" --env-file "$ENV_FILE" ps
if [ -f "$ROOT_DIR/agent-registry/compose.yaml" ]; then
  docker compose --project-directory "$ROOT_DIR/agent-registry" -f "$ROOT_DIR/agent-registry/compose.yaml" --env-file "$ENV_FILE" ps
fi
printf '\nPlanning Agent: '; curl --fail --silent "http://127.0.0.1:${PLANNING_AGENT_PORT:-18000}/health" || true
printf '\nGateway: '; curl --fail --silent "http://127.0.0.1:${GATEWAY_PORT:-27880}/healthz" || true
printf '\n'
