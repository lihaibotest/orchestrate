#!/usr/bin/env bash
set -euo pipefail
PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${ORCHESTRATOR_ENV_FILE:-$PACKAGE_DIR/.env}"
if [ ! -f "$ENV_FILE" ]; then
  echo "missing orchestrator env file: $ENV_FILE" >&2
  exit 2
fi
set -a
. "$ENV_FILE"
set +a
"$PACKAGE_DIR/scripts/verify-external-dependencies.sh"
"$PACKAGE_DIR/scripts/verify-package.sh" --load
docker compose --project-directory "$PACKAGE_DIR" -f "$PACKAGE_DIR/docker-compose.yml" --env-file "$ENV_FILE" up -d --no-build
