#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${MIGRATION_ENV_FILE:-$ROOT_DIR/.env}"
docker compose --project-directory "$ROOT_DIR" -f "$ROOT_DIR/docker-compose.migration.yml" --env-file "$ENV_FILE" down
if [ -f "$ROOT_DIR/agent-registry/compose.yaml" ]; then
  docker compose --project-directory "$ROOT_DIR/agent-registry" -f "$ROOT_DIR/agent-registry/compose.yaml" --env-file "$ENV_FILE" down
fi
