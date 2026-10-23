#!/usr/bin/env bash
# Start the three host-based external Agents used by the validated workflow.
#
# This script deliberately targets the host services, not the similarly named
# legacy Docker Agent containers.  It is safe to run repeatedly: an Agent
# whose TCP port is already listening is left untouched.
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PID_DIR="$ROOT_DIR/state/external-agents"

WORD_DIR='/home/test/MinIO（复件）/test_word_gener_update_v5.0'
WORKER_DIR='/home/test/MinIO（复件）/worker-1'
REPORT_DIR='/home/test/MinIO（复件）/report_agent'
LOG_ROOT='/home/test/MinIO（复件）/logs'

WORD_PYTHON='/home/test/anaconda3/envs/word_gen/bin/python'
WORKER_PYTHON='/home/test/anaconda3/envs/pytest/bin/python'
REPORT_PYTHON='/home/test/anaconda3/envs/report/bin/python'

usage() {
  cat <<'EOF'
Usage: scripts/start-external-agents.sh [start|status]

start   Start Word, Worker, and Report host Agents (default).
status  Show their listener/PID state without changing anything.
EOF
}

port_is_listening() {
  local port="$1"
  ss -ltnH 2>/dev/null | awk -v port=":$port" '$4 ~ (port "$") { found=1 } END { exit !found }'
}

pid_is_alive() {
  local pid_file="$1"
  test -s "$pid_file" && kill -0 "$(<"$pid_file")" 2>/dev/null
}

show_status() {
  local name="$1" port="$2" pid_file="$3"
  if port_is_listening "$port"; then
    printf '%-12s listening on %s' "$name" "$port"
  else
    printf '%-12s not listening on %s' "$name" "$port"
  fi
  if pid_is_alive "$pid_file"; then
    printf ' (launcher pid %s)' "$(<"$pid_file")"
  fi
  printf '\n'
}

start_agent() {
  local name="$1" workdir="$2" python_bin="$3" entrypoint="$4" port="$5" log_file="$6"
  local pid_file="$PID_DIR/$name.pid"
  local attempt

  if port_is_listening "$port"; then
    printf '%s is already listening on port %s; leaving it running.\n' "$name" "$port"
    return 0
  fi
  if pid_is_alive "$pid_file"; then
    printf '%s launcher PID %s is alive but port %s is not ready; not starting a duplicate.\n' \
      "$name" "$(<"$pid_file")" "$port" >&2
    return 1
  fi
  rm -f "$pid_file"
  test -d "$workdir" || { echo "missing $name directory: $workdir" >&2; return 1; }
  test -x "$python_bin" || { echo "missing $name Python: $python_bin" >&2; return 1; }
  test -f "$workdir/$entrypoint" || { echo "missing $name entrypoint: $workdir/$entrypoint" >&2; return 1; }

  mkdir -p "$(dirname -- "$log_file")"
  (
    cd "$workdir"
    exec nohup env PYTHONUNBUFFERED=1 "$python_bin" "$entrypoint" >>"$log_file" 2>&1
  ) &
  printf '%s\n' "$!" >"$pid_file"

  for attempt in $(seq 1 30); do
    if port_is_listening "$port"; then
      printf '%s started (PID %s, port %s, log %s).\n' "$name" "$(<"$pid_file")" "$port" "$log_file"
      return 0
    fi
    if ! pid_is_alive "$pid_file"; then
      echo "$name exited before listening; inspect $log_file" >&2
      rm -f "$pid_file"
      return 1
    fi
    sleep 1
  done

  echo "$name did not listen on port $port within 30 seconds; inspect $log_file" >&2
  return 1
}

main() {
  local command="${1:-start}"
  test "$#" -le 1 || { usage >&2; exit 2; }
  mkdir -p "$PID_DIR"
  umask 077

  case "$command" in
    status)
      show_status word-agent 4324 "$PID_DIR/word-agent.pid"
      show_status worker 5000 "$PID_DIR/worker.pid"
      show_status report-agent 6000 "$PID_DIR/report-agent.pid"
      ;;
    start)
      start_agent word-agent "$WORD_DIR" "$WORD_PYTHON" api_service.py 4324 "$LOG_ROOT/word_agent/word_agent.log"
      start_agent worker "$WORKER_DIR" "$WORKER_PYTHON" pytest_api.py 5000 "$LOG_ROOT/worker/worker-1.log"
      start_agent report-agent "$REPORT_DIR" "$REPORT_PYTHON" web_app.py 6000 "$LOG_ROOT/report_agent/report_agent.log"
      echo 'All external Agent ports are ready.'
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
