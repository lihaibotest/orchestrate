#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

# P0-01 owns the Python 3.10 runtime/lint/type/unit gate. Reuse it so the
# legacy FastAPI path is never accidentally tested with the host Python 3.8.
PYTHON_BIN="${P0_02_PYTHON_BIN:-python3}" bash "$ROOT_DIR/scripts/check-p0-01.sh"

# Run the PostgreSQL contract tests only with the confirmed Python 3.10+
# baseline. The vendored wheel keeps this gate usable in the offline bundle.
if [[ -n "${P0_02_POSTGRES_DSN:-}" ]]; then
  INTEGRATION_PYTHON="${P0_02_PYTHON_BIN:-python3}"
  if ! command -v "$INTEGRATION_PYTHON" >/dev/null 2>&1 \
    || ! "$INTEGRATION_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
  then
    echo "P0-02 integration tests require Python 3.10+ (set P0_02_PYTHON_BIN)" >&2
    exit 2
  fi
  PYTHONPATH="$ROOT_DIR/orchestrator/vendor/python:$ROOT_DIR/orchestrator/backend${PYTHONPATH:+:$PYTHONPATH}" \
    P0_01_PROJECT_ROOT="$ROOT_DIR" \
    "$INTEGRATION_PYTHON" -m unittest discover -s "$ROOT_DIR/tests/p0_02" -p 'test_*.py' -v
else
  echo "P0-02 PostgreSQL integration test not run: set P0_02_POSTGRES_DSN" >&2
fi
