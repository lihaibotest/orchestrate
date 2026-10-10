#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TEST_ROOT="$(CDPATH= cd -- "$PACKAGE_DIR/../../.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1

pytest -q -p no:cacheprovider "$PACKAGE_DIR/tests"
ORCHESTRATOR_PACKAGE_UNDER_TEST="$PACKAGE_DIR" pytest -q -p no:cacheprovider "$TEST_ROOT/tests"
pytest -q -p no:cacheprovider "$PACKAGE_DIR/tests/test_development_bundle_contents.py"

if [ -d "$PACKAGE_DIR/artifacts" ]; then
  "$PACKAGE_DIR/scripts/verify-package.sh"
fi
