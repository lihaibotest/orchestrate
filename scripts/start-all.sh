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
# The bundled n8n worker may be managed by the external agent stack. Attach it
# to every network used by the registry agents so HTTP Request nodes can resolve
# wordagent/pytestproject/reportagent after a host restart.
AGENT_WORD_CONTAINER="${AGENT_WORD_CONTAINER:-wordagent}"
if ! docker inspect "$AGENT_WORD_CONTAINER" >/dev/null 2>&1; then
  AGENT_WORD_CONTAINER="$(docker ps --filter name=wordagent --format '{{.Names}}' | head -1)"
fi
if docker inspect worker-fresh >/dev/null 2>&1 && [ -n "$AGENT_WORD_CONTAINER" ]; then
  for agent_network in $(docker inspect -f '{{range $name, $config := .NetworkSettings.Networks}}{{$name}} {{end}}' "$AGENT_WORD_CONTAINER" 2>/dev/null || true); do
    docker network connect "$agent_network" worker-fresh >/dev/null 2>&1 || true
  done
fi
for service in postgres n8n wordagent pytestproject reportagent filter-library-api filter-library-ui; do
  "${REGISTRY_COMPOSE[@]}" ps --status running "$service" >/dev/null 2>&1 || true
done
if [ -x "$REGISTRY_ROOT/scripts/provision-n8n.sh" ]; then "$REGISTRY_ROOT/scripts/provision-n8n.sh"; fi
if [ -x "$REGISTRY_ROOT/scripts/register-agents.sh" ]; then "$REGISTRY_ROOT/scripts/register-agents.sh"; fi
"${COMPOSE[@]}" up -d --no-build
curl --fail --silent --show-error --retry 20 --retry-delay 1 "http://127.0.0.1:${PLANNING_AGENT_PORT:-18000}/health" >/dev/null
curl --fail --silent --show-error --retry 20 --retry-delay 1 "http://127.0.0.1:${GATEWAY_PORT:-27880}/healthz" >/dev/null
"${COMPOSE[@]}" ps
