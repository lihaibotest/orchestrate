#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
POSTGRES_DSN="${P0_09_POSTGRES_DSN:-${P0_08_POSTGRES_DSN:-${P0_07_POSTGRES_DSN:-${P0_06_POSTGRES_DSN:-${P0_05_POSTGRES_DSN:-${P0_04_POSTGRES_DSN:-${P0_03_POSTGRES_DSN:-${P0_02_POSTGRES_DSN:-}}}}}}}}"
CHECK_PYTHON="${P0_09_PYTHON_BIN:-${P0_08_PYTHON_BIN:-${P0_07_PYTHON_BIN:-${P0_06_PYTHON_BIN:-${P0_05_PYTHON_BIN:-${P0_04_PYTHON_BIN:-${P0_03_PYTHON_BIN:-${P0_02_PYTHON_BIN:-python3}}}}}}}}"
VENDORED_PYTHON="$ROOT_DIR/orchestrator/vendor/python"

if [[ -z "$POSTGRES_DSN" ]]; then
  echo "P0-09 requires P0_09_POSTGRES_DSN or a prior P0 PostgreSQL DSN" >&2
  exit 2
fi

env -u P0_09_POSTGRES_DSN \
  P0_08_POSTGRES_DSN="$POSTGRES_DSN" \
  P0_08_PYTHON_BIN="$CHECK_PYTHON" \
  bash "$ROOT_DIR/scripts/check-p0-08.sh"

PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  P0_01_PROJECT_ROOT="$ROOT_DIR" \
  P0_BLUEPRINT_POSTGRES_DSN="$POSTGRES_DSN" \
  "$CHECK_PYTHON" -m unittest discover \
    -s "$ROOT_DIR/tests/planning_blueprint_v3" -p 'test_*.py' -v

PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  P0_01_PROJECT_ROOT="$ROOT_DIR" \
  P0_09_POSTGRES_DSN="$POSTGRES_DSN" \
  "$CHECK_PYTHON" -m unittest discover \
    -s "$ROOT_DIR/tests/p0_09" -p 'test_*.py' -v
