#!/usr/bin/env bash
set -euo pipefail
PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${ORCHESTRATOR_ENV_FILE:-$PACKAGE_DIR/.env}"
docker compose --project-directory "$PACKAGE_DIR" -f "$PACKAGE_DIR/docker-compose.yml" --env-file "$ENV_FILE" down
