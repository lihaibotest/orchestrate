#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
POSTGRES_DSN="${P0_10_POSTGRES_DSN:-${P0_09_POSTGRES_DSN:-}}"
CHECK_PYTHON="${P0_10_PYTHON_BIN:-${P0_09_PYTHON_BIN:-/home/test/anaconda3/envs/SCG-Agent/bin/python}}"
VENDORED_PYTHON="$ROOT_DIR/orchestrator/vendor/python"

if [[ -z "$POSTGRES_DSN" ]]; then
  echo "P0-10 requires P0_10_POSTGRES_DSN or P0_09_POSTGRES_DSN" >&2
  exit 2
fi
if ! command -v "$CHECK_PYTHON" >/dev/null 2>&1; then
  echo "P0-10 requires Python 3.10+ (set P0_10_PYTHON_BIN)" >&2
  exit 2
fi

P0_09_POSTGRES_DSN="$POSTGRES_DSN" \
P0_09_PYTHON_BIN="$CHECK_PYTHON" \
PYTHON_BIN="$CHECK_PYTHON" \
bash "$ROOT_DIR/scripts/check-p0-09.sh"

PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  P0_01_PROJECT_ROOT="$ROOT_DIR" \
  P0_10_POSTGRES_DSN="$POSTGRES_DSN" \
  "$CHECK_PYTHON" -m unittest discover -s "$ROOT_DIR/tests/p0_10" -p 'test_*.py' -v
