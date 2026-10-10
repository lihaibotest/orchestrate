#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
POSTGRES_DSN="${P0_03_POSTGRES_DSN:-${P0_02_POSTGRES_DSN:-}}"
INTEGRATION_PYTHON="${P0_03_PYTHON_BIN:-${P0_02_PYTHON_BIN:-python3}}"

if [[ -z "$POSTGRES_DSN" ]]; then
  echo "P0-03 requires P0_03_POSTGRES_DSN or P0_02_POSTGRES_DSN" >&2
  exit 2
fi

env -u P0_03_POSTGRES_DSN \
  P0_02_POSTGRES_DSN="$POSTGRES_DSN" \
  P0_02_PYTHON_BIN="$INTEGRATION_PYTHON" \
  bash "$ROOT_DIR/scripts/check-p0-02.sh"

if ! command -v "$INTEGRATION_PYTHON" >/dev/null 2>&1 \
  || ! "$INTEGRATION_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
then
  echo "P0-03 integration tests require Python 3.10+ (set P0_03_PYTHON_BIN)" >&2
  exit 2
fi

PYTHONPATH="$ROOT_DIR/orchestrator/vendor/python:$ROOT_DIR/orchestrator/backend${PYTHONPATH:+:$PYTHONPATH}" \
  P0_01_PROJECT_ROOT="$ROOT_DIR" \
  P0_03_POSTGRES_DSN="$POSTGRES_DSN" \
  "$INTEGRATION_PYTHON" -m unittest discover -s "$ROOT_DIR/tests/p0_03" -p 'test_*.py' -v
