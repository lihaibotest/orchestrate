#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
POSTGRES_DSN="${P0_06_POSTGRES_DSN:-${P0_05_POSTGRES_DSN:-${P0_04_POSTGRES_DSN:-${P0_03_POSTGRES_DSN:-${P0_02_POSTGRES_DSN:-}}}}}"
CHECK_PYTHON="${P0_06_PYTHON_BIN:-${P0_05_PYTHON_BIN:-${P0_04_PYTHON_BIN:-${P0_03_PYTHON_BIN:-${P0_02_PYTHON_BIN:-python3}}}}}"
VENDORED_PYTHON="$ROOT_DIR/orchestrator/vendor/python"

if [[ -z "$POSTGRES_DSN" ]]; then
  echo "P0-06 requires P0_06_POSTGRES_DSN or a prior P0 PostgreSQL DSN" >&2
  exit 2
fi

env -u P0_06_POSTGRES_DSN \
  PYTHON_BIN="$CHECK_PYTHON" \
  PYTHONPATH="$VENDORED_PYTHON${PYTHONPATH:+:$PYTHONPATH}" \
  P0_05_POSTGRES_DSN="$POSTGRES_DSN" \
  P0_05_PYTHON_BIN="$CHECK_PYTHON" \
  bash "$ROOT_DIR/scripts/check-p0-05.sh"

if ! command -v "$CHECK_PYTHON" >/dev/null 2>&1 \
  || ! "$CHECK_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
then
  echo "P0-06 tests require Python 3.10+ (set P0_06_PYTHON_BIN)" >&2
  exit 2
fi

PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend${PYTHONPATH:+:$PYTHONPATH}" \
  P0_01_PROJECT_ROOT="$ROOT_DIR" \
  "$CHECK_PYTHON" -m unittest discover -s "$ROOT_DIR/tests/p0_06" -p 'test_*.py' -v
