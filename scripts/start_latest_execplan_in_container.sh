#!/usr/bin/env bash
# Run start_latest_execplan.py inside the execution-orchestrator container.
#
# The configured PostgreSQL host (pg-fresh) is resolvable on the Docker network,
# not from the host. This wrapper preserves the container's existing DSN and
# submit-token configuration; it never prints either secret.
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONTAINER=${EXECUTION_ORCHESTRATOR_CONTAINER:-p009-execution-service}
CONTAINER_SCRIPT=/dev/shm/p009-start_latest_execplan.py
CONTAINER_INPUTS=

usage() {
  cat <<'EOF'
Usage:
  scripts/start_latest_execplan_in_container.sh [--dry-run]
  scripts/start_latest_execplan_in_container.sh [--plan-id ID --plan-version N]
  scripts/start_latest_execplan_in_container.sh --start --inputs-file /absolute/host/path.json

The default selects the newest durable ExecPlan. Use --plan-id and
--plan-version when another plan might arrive concurrently.
EOF
}

ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --inputs-file)
      [ "$#" -ge 2 ] || { echo "error: --inputs-file requires a file path" >&2; exit 2; }
      case "$2" in
        /*) ;;
        *) echo "error: --inputs-file must be an absolute host path" >&2; exit 2 ;;
      esac
      [ -f "$2" ] || { echo "error: inputs file does not exist: $2" >&2; exit 2; }
      CONTAINER_INPUTS=/dev/shm/p009-runtime-inputs.json
      ARGS+=(--inputs-file "$CONTAINER_INPUTS")
      INPUTS_HOST=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

# docker cp cannot reliably populate this container's tmpfs mount. Stream the
# two explicitly selected local files into its writable /dev/shm instead.
docker exec -i "$CONTAINER" /usr/local/bin/python -c \
  "import pathlib,sys; pathlib.Path('$CONTAINER_SCRIPT').write_bytes(sys.stdin.buffer.read())" \
  < "$ROOT_DIR/scripts/start_latest_execplan.py"
if [ -n "$CONTAINER_INPUTS" ]; then
  docker exec -i "$CONTAINER" /usr/local/bin/python -c \
    "import pathlib,sys; pathlib.Path('$CONTAINER_INPUTS').write_bytes(sys.stdin.buffer.read())" \
    < "$INPUTS_HOST"
fi

exec docker exec "$CONTAINER" /usr/local/bin/python "$CONTAINER_SCRIPT" \
  --endpoint http://127.0.0.1:8020 \
  --submit-token-file /run/secrets/execution-submit-token \
  "${ARGS[@]}"
