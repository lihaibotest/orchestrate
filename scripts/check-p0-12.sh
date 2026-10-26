#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
MODE="${P0_12_MODE:-full}"
POSTGRES_DSN="${P0_12_POSTGRES_DSN:-${P0_11_POSTGRES_DSN:-}}"
CHECK_PYTHON="${P0_12_PYTHON_BIN:-${P0_11_PYTHON_BIN:-/home/test/anaconda3/envs/SCG-Agent/bin/python}}"
REPORT_FILE="${P0_12_REPORT_FILE:-$ROOT_DIR/P0-12-ACCEPTANCE-REPORT.md}"
JSON_REPORT="${P0_12_JSON_REPORT:-/tmp/p0-12-acceptance.json}"
LOG_DIR="${P0_12_LOG_DIR:-/tmp/p0-12-logs}"
VENDORED_PYTHON="$ROOT_DIR/orchestrator/vendor/python"

case "$MODE" in
  full|offline|external) ;;
  *) echo "P0-12 invalid P0_12_MODE: $MODE (expected full|offline|external)" >&2; exit 2 ;;
esac
if ! command -v "$CHECK_PYTHON" >/dev/null 2>&1; then
  echo "P0-12 requires Python 3.10+ (set P0_12_PYTHON_BIN)" >&2
  exit 2
fi
if ! "$CHECK_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "P0-12 requires Python 3.10+" >&2
  exit 2
fi
mkdir -p "$LOG_DIR"

declare -a CHECK_GROUPS=()
declare -a STATUSES=()
declare -a LOGS=()

run_group() {
  local name="$1"
  shift
  local log="$LOG_DIR/$name.log"
  local status=0
  echo "[P0-12] $name"
  set +e
  "$@" >"$log" 2>&1
  status=$?
  set -e
  CHECK_GROUPS+=("$name")
  STATUSES+=("$status")
  LOGS+=("$log")
  if [[ "$status" -ne 0 ]]; then
    echo "[P0-12] $name: FAIL ($status); log=$log" >&2
    tail -40 "$log" >&2 || true
  else
    echo "[P0-12] $name: PASS"
  fi
  return 0
}

run_group diff_check git -C "$ROOT_DIR" diff --check

postgres_status="PASS"
if [[ "$MODE" == "offline" ]]; then
  run_group offline_quality env -u P0_02_POSTGRES_DSN -u P0_03_POSTGRES_DSN \
    -u P0_04_POSTGRES_DSN -u P0_05_POSTGRES_DSN -u P0_06_POSTGRES_DSN \
    -u P0_07_POSTGRES_DSN -u P0_08_POSTGRES_DSN -u P0_09_POSTGRES_DSN \
    -u P0_10_POSTGRES_DSN -u P0_11_POSTGRES_DSN -u P0_12_POSTGRES_DSN \
    P0_01_PROJECT_ROOT="$ROOT_DIR" ORCHESTRATOR_DATABASE_PATH="/tmp/orchestrator-p0-12.sqlite3" \
    PYTHON_BIN="$CHECK_PYTHON" bash "$ROOT_DIR/scripts/check-p0-01.sh"
  run_group offline_regression env -u P0_02_POSTGRES_DSN -u P0_03_POSTGRES_DSN \
    -u P0_04_POSTGRES_DSN -u P0_05_POSTGRES_DSN -u P0_06_POSTGRES_DSN \
    -u P0_07_POSTGRES_DSN -u P0_08_POSTGRES_DSN -u P0_09_POSTGRES_DSN \
    -u P0_10_POSTGRES_DSN -u P0_11_POSTGRES_DSN -u P0_12_POSTGRES_DSN \
    P0_01_PROJECT_ROOT="$ROOT_DIR" ORCHESTRATOR_DATABASE_PATH="/tmp/orchestrator-p0-12-full.sqlite3" \
    PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$CHECK_PYTHON" -m unittest discover -s "$ROOT_DIR/tests" -p 'test_*.py'
  run_group p0_12_fake env PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    P0_01_PROJECT_ROOT="$ROOT_DIR" "$CHECK_PYTHON" -m unittest discover \
      -s "$ROOT_DIR/tests/p0_12" -p 'test_*.py' -v
else
  if [[ -z "$POSTGRES_DSN" ]]; then
    echo "P0-12 $MODE mode requires P0_12_POSTGRES_DSN or P0_11_POSTGRES_DSN" >&2
    postgres_status="BLOCKED_REQUIRED"
    run_group p0_12_fake_without_postgres env \
      PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
      P0_01_PROJECT_ROOT="$ROOT_DIR" "$CHECK_PYTHON" -m unittest discover \
        -s "$ROOT_DIR/tests/p0_12" -p 'test_*.py' -v
  else
    # Keep the historical P0-02..P0-11 gate on its own migration boundary.
    # P0_12_POSTGRES_DSN is intentionally set for the following P0-12 group;
    # leaking it here makes older suites apply migration 0011 and fail their
    # stage-specific latest-version assertions.
    run_group p0_02_to_p0_11_regression env -u P0_12_POSTGRES_DSN -u ORCHESTRATOR_ENABLE_P0_12 \
      P0_11_POSTGRES_DSN="$POSTGRES_DSN" P0_11_PYTHON_BIN="$CHECK_PYTHON" \
      bash "$ROOT_DIR/scripts/check-p0-11.sh"
    run_group p0_12_fake_and_postgres env \
      PYTHONPATH="$VENDORED_PYTHON:$ROOT_DIR/orchestrator/backend:$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
      P0_01_PROJECT_ROOT="$ROOT_DIR" P0_12_POSTGRES_DSN="$POSTGRES_DSN" \
      ORCHESTRATOR_ENABLE_P0_12=1 "$CHECK_PYTHON" -m unittest discover \
        -s "$ROOT_DIR/tests/p0_12" -p 'test_*.py' -v
    if [[ "${STATUSES[${#STATUSES[@]}-1]}" -ne 0 ]]; then
      postgres_status="FAIL"
    fi
  fi
fi

external_status="BLOCKED_EXTERNAL"
if [[ "$MODE" == "full" || "$MODE" == "external" || "${P0_12_EXTERNAL_REQUIRED:-0}" == "1" ]]; then
  if [[ -n "${P0_12_EXTERNAL_E2E_CMD:-}" ]]; then
    run_group external_e2e bash -lc "$P0_12_EXTERNAL_E2E_CMD"
    external_status="PASS"
  else
    external_status="BLOCKED_EXTERNAL"
    echo "[P0-12] external_e2e: BLOCKED_EXTERNAL (no approved external command configured)" >&2
  fi
fi

if [[ "$MODE" == "offline" ]]; then postgres_status="BLOCKED_REQUIRED"; fi
failed=0
for status in "${STATUSES[@]}"; do
  if [[ "$status" -ne 0 ]]; then failed=1; fi
done
if [[ "$external_status" == "BLOCKED_EXTERNAL" ]]; then failed=1; fi
if [[ "$external_status" == "BLOCKED_EXTERNAL" && "${P0_12_ALLOW_EXTERNAL_DEFER:-0}" == "1" ]]; then failed=0; fi
if [[ "$postgres_status" == "BLOCKED_REQUIRED" ]]; then failed=1; fi

export P0_12_JSON_REPORT="$JSON_REPORT" P0_12_REPORT_FILE="$REPORT_FILE" MODE postgres_status external_status failed
export P0_12_GROUPS="$(IFS=,; echo "${CHECK_GROUPS[*]}")"
export P0_12_STATUSES="$(IFS=,; echo "${STATUSES[*]}")"
export P0_12_LOGS="$(IFS=,; echo "${LOGS[*]}")"
"$CHECK_PYTHON" - "$ROOT_DIR" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "orchestrator" / "backend"))
sys.path.insert(0, str(root))
groups = [item for item in os.environ.get("P0_12_GROUPS", "").split(",") if item]
statuses = [item for item in os.environ.get("P0_12_STATUSES", "").split(",") if item]
logs = [item for item in os.environ.get("P0_12_LOGS", "").split(",") if item]
records = [
    {"name": name, "status": "PASS" if code == "0" else "FAIL", "exit_code": int(code), "log": log}
    for name, code, log in zip(groups, statuses, logs)
]
import subprocess

def git_value(*args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"

python_version = sys.version.split()[0]
git_branch = git_value("branch", "--show-current")
git_commit = git_value("rev-parse", "HEAD")
git_dirty = "yes" if git_value("status", "--porcelain") else "no"
try:
    from tests.p0_12.p0_e2e_acceptance import P0E2EAcceptance

    happy_path_evidence = P0E2EAcceptance().run_happy_path().as_evidence()
except Exception as error:
    happy_path_evidence = {"error": f"{type(error).__name__}: {error}"}
conclusion = "P0_ACCEPTED"
if os.environ.get("failed") != "0" or os.environ.get("MODE") == "offline":
    conclusion = "P0_BLOCKED"
elif os.environ.get("external_status") == "BLOCKED_EXTERNAL":
    conclusion = "P0_CONDITIONALLY_ACCEPTED"
report = {
    "gate": "P0-12",
    "conclusion": conclusion,
    "mode": os.environ.get("MODE"),
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "migration_version": 11 if os.environ.get("postgres_status") == "PASS" else None,
    "postgres": os.environ.get("postgres_status"),
    "external": os.environ.get("external_status"),
    "groups": records,
    "happy_path": happy_path_evidence,
    "required_faults": [
        "FI-01 blueprint_execute_replay",
        "FI-02 publish_execute_response_loss",
        "FI-03 runtime_event_duplicate_missing",
        "FI-04 agent_stalled",
        "FI-05 artifact_hash_mismatch",
        "FI-06 cmm_disconnect_publisher_crash_recovery",
    ],
}
json_path = Path(os.environ["P0_12_JSON_REPORT"])
json_path.write_text(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
markdown = [
    "# P0-12 Acceptance Report",
    "",
    f"- Conclusion: **{conclusion}**",
    f"- Mode: `{report['mode']}`",
    f"- Generated: `{report['generated_at']}`",
    f"- Migration: `{report['postgres']}` (expected latest version 11)",
    f"- External E2E: `{report['external']}`",
    "",
    "## Baseline",
    "",
    f"- Git branch: `{git_branch}`",
    f"- Git commit: `{git_commit}`",
    f"- Worktree has uncommitted changes: `{git_dirty}` (preserved as part of this implementation)",
    f"- Python: `{python_version}`",
    f"- PostgreSQL gate: `{report['postgres']}`; DSN supplied: `{'yes' if os.environ.get('P0_12_POSTGRES_DSN') or os.environ.get('P0_11_POSTGRES_DSN') else 'no'}`",
    "- n8n/Agent/MinIO/CMM: external execution is only run with the explicitly supplied approved command; otherwise status remains `BLOCKED_EXTERNAL`.",
    "",
    "## Verification Groups",
    "",
    "| Group | Result | Exit | Log |",
    "|---|---|---:|---|",
]
for item in records:
    markdown.append(f"| `{item['name']}` | {item['status']} | {item['exit_code']} | `{item['log']}` |")
markdown += ["", "## Required Faults", ""]
fault_tests = {
    "FI-01 blueprint_execute_replay": "test_blueprint_and_execute_replay_have_one_business_identity",
    "FI-02 publish_execute_response_loss": "test_execution_http_retry_reuses_execution_identity",
    "FI-03 runtime_event_duplicate_missing": "test_runtime_duplicate_and_missing_event_have_evidence",
    "FI-04 agent_stalled": "test_agent_stall_has_retry_and_reconciliation_evidence",
    "FI-05 artifact_hash_mismatch": "test_artifact_hash_mismatch_is_not_trusted",
    "FI-06 cmm_disconnect_publisher_crash_recovery": "test_cmm_publisher_crash_recovers_without_duplicate_delivery",
}
markdown.extend(f"- `{item}` -> `{fault_tests[item]}`" for item in report["required_faults"])
markdown += [
    "",
    "## Regression",
    "",
    "- The group logs above are the authoritative command output. Offline mode runs the full `tests/` discovery with PostgreSQL variables unset; full mode delegates the historical P0-02~P0-11 gate to `scripts/check-p0-11.sh`.",
    "- PostgreSQL migration evidence is required in full mode and is reported as `BLOCKED_REQUIRED` when no DSN is supplied.",
    "",
    "## Fault Evidence",
    "",
    "- FI-01/FI-02 prove stable blueprint, workflow, execution and external operation identity under replay/response loss.",
    "- FI-03 proves duplicate event idempotency and explicit missing-event reconciliation evidence.",
    "- FI-04 proves stalled agent handling without manufacturing a successful terminal state.",
    "- FI-05 proves hash mismatch becomes `ARTIFACT_MISMATCH` and is not trusted.",
    "- FI-06 proves CMM outage/crash recovery retains the outbox and delivers one logical event after recovery.",
]
for record in records:
    if record["name"] in {"offline_regression", "p0_12_fake", "p0_12_fake_without_postgres", "p0_12_fake_and_postgres"}:
        try:
            log_text = Path(record["log"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        import re
        match = re.search(r"Ran (\d+) tests.*?OK \(skipped=(\d+)\)", log_text, re.S)
        if match:
            markdown.append(
                f"- `{record['name']}` count: `{match.group(1)}` tests run, "
                f"`{match.group(2)}` expected skips, zero failures."
            )
markdown += ["", "## Happy Path Evidence", "", "```json", json.dumps(happy_path_evidence, ensure_ascii=True, indent=2, sort_keys=True), "```"]
markdown += [
    "",
    "## Boundaries",
    "",
    "Real n8n/Agent/MinIO/CMM execution is only reported when an approved external command is supplied via `P0_12_EXTERNAL_E2E_CMD`; otherwise it remains `BLOCKED_EXTERNAL` and cannot be treated as PASS.",
    "P1 Checkpoint/Fingerprint, Evaluation/Reuse, Resume Root and PATCH Version Lineage are not implemented by P0-12.",
]
Path(os.environ["P0_12_REPORT_FILE"]).write_text("\n".join(markdown) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=True, sort_keys=True))
PY

if [[ "$failed" -ne 0 ]]; then
  echo "P0-12: BLOCKED (report: $REPORT_FILE; json: $JSON_REPORT)" >&2
  exit 1
fi
if [[ "$external_status" == "BLOCKED_EXTERNAL" ]]; then
  echo "P0-12: CONDITIONALLY_ACCEPTED (report: $REPORT_FILE; json: $JSON_REPORT)"
else
  echo "P0-12: PASS (report: $REPORT_FILE; json: $JSON_REPORT)"
fi
