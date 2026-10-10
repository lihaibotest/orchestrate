#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
IMAGE="${P0_01_RUNTIME_IMAGE:-orchestrator-agent-standalone:1.1.0}"

if command -v "$PYTHON_BIN" >/dev/null 2>&1 \
  && "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  && PYTHONPATH="$ROOT_DIR/orchestrator/backend" "$PYTHON_BIN" -c 'import fastapi, pydantic'
then
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONPATH="$ROOT_DIR/orchestrator/backend${PYTHONPATH:+:$PYTHONPATH}"
  export P0_01_PROJECT_ROOT="$ROOT_DIR"
  export ORCHESTRATOR_DATABASE_PATH="${ORCHESTRATOR_DATABASE_PATH:-/tmp/orchestrator-p0-01.sqlite3}"
  exec "$PYTHON_BIN" "$ROOT_DIR/scripts/run-p0-01.py"
fi

if command -v docker >/dev/null 2>&1 && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  exec docker run --rm \
    --entrypoint /usr/local/bin/python3.10 \
    -e P0_01_PROJECT_ROOT=/workspace \
    -e PYTHONPATH=/workspace/orchestrator/backend \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$ROOT_DIR:/workspace:ro" \
    -v /tmp:/tmp \
    "$IMAGE" /workspace/scripts/run-p0-01.py
fi

echo "Python 3.10 with FastAPI/Pydantic, or image $IMAGE, is required" >&2
exit 2
