#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${MIGRATION_ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE=(docker compose --project-directory "$ROOT_DIR" -f "$ROOT_DIR/docker-compose.migration.yml" --env-file "$ENV_FILE")
REGISTRY_ROOT="$ROOT_DIR/agent-registry"
REGISTRY_COMPOSE=(docker compose --project-directory "$REGISTRY_ROOT" -f "$REGISTRY_ROOT/compose.yaml" --env-file "$ENV_FILE")
"$ROOT_DIR/scripts/check-host-prerequisites.sh"
test -f "$ENV_FILE" || { echo "missing environment file: $ENV_FILE (copy config/.env.example to .env and set runtime values)" >&2; exit 1; }
test -f "$REGISTRY_ROOT/compose.yaml" || { echo "missing bundled Agent Registry project" >&2; exit 1; }
mkdir -p "$ROOT_DIR"/{state,logs,data,config/.runtime-secrets}
"$ROOT_DIR/scripts/prepare-runtime-secrets.sh"
"$ROOT_DIR/scripts/verify-migration-bundle.sh"
NETWORK_NAME="${REGISTRY_NETWORK_NAME:-orchestrator_shared}"
docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || docker network create "$NETWORK_NAME" >/dev/null
if [ -x "$ROOT_DIR/scripts/load-migration-images.sh" ]; then "$ROOT_DIR/scripts/load-migration-images.sh"; fi
"${REGISTRY_COMPOSE[@]}" up -d --no-build
for service in postgres n8n wordagent pytestproject reportagent filter-library-api filter-library-ui; do
  "${REGISTRY_COMPOSE[@]}" ps --status running "$service" >/dev/null 2>&1 || true
done
if [ -x "$REGISTRY_ROOT/scripts/provision-n8n.sh" ]; then "$REGISTRY_ROOT/scripts/provision-n8n.sh"; fi
if [ -x "$REGISTRY_ROOT/scripts/register-agents.sh" ]; then "$REGISTRY_ROOT/scripts/register-agents.sh"; fi
"${COMPOSE[@]}" up -d --no-build
curl --fail --silent --show-error --retry 20 --retry-delay 1 "http://127.0.0.1:${PLANNING_AGENT_PORT:-18000}/health" >/dev/null
curl --fail --silent --show-error --retry 20 --retry-delay 1 "http://127.0.0.1:${GATEWAY_PORT:-27880}/healthz" >/dev/null
"${COMPOSE[@]}" ps
