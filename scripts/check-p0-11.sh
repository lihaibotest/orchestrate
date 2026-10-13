#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
POSTGRES_DSN="${P0_11_POSTGRES_DSN:-}"
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_10_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_09_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_08_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_07_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_06_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_05_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_04_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_03_POSTGRES_DSN:-}"; fi
if [[ -z "$POSTGRES_DSN" ]]; then POSTGRES_DSN="${P0_02_POSTGRES_DSN:-}"; fi
CHECK_PYTHON="${P0_11_PYTHON_BIN:-}"
if [[ -z "$CHECK_PYTHON" ]]; then CHECK_PYTHON="${P0_10_PYTHON_BIN:-/home/test/anaconda3/envs/SCG-Agent/bin/python}"; fi
VENDORED_PYTHON="$ROOT_DIR/orchestrator/vendor/python"

if [[ -z "$POSTGRES_DSN" ]]; then
  echo "P0-11 requires P0_11_POSTGRES_DSN or a prior P0 PostgreSQL DSN" >&2
  exit 2
fi

# Historical gates intentionally run without the opt-in 0010 migration.
env -u P0_11_POSTGRES_DSN -u ORCHESTRATOR_ENABLE_P0_11 \
  P0_10_POSTGRES_DSN="$POSTGRES_DSN" P0_10_PYTHON_BIN="$CHECK_PYTHON" \
  bash "$ROOT_DIR/scripts/check-p0-10.sh"

PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  P0_11_POSTGRES_DSN="$POSTGRES_DSN" ORCHESTRATOR_ENABLE_P0_11=1 \
  P0_01_PROJECT_ROOT="$ROOT_DIR" \
  "$CHECK_PYTHON" -m unittest discover -s "$ROOT_DIR/tests/p0_11" -p 'test_*.py' -v
