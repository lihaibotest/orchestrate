#!/usr/bin/env bash
set -euo pipefail
: "${N8N_BASE_URL:?N8N_BASE_URL is required}"
: "${ORCHESTRATOR_PLANNING_AGENT_BASE_URL:?planning agent URL is required}"
: "${ORCHESTRATOR_WORKFLOW_LLM_BASE_URL:?workflow LLM URL is required}"
endpoints=("$N8N_BASE_URL/healthz" "$ORCHESTRATOR_PLANNING_AGENT_BASE_URL/health")
for endpoint in "${endpoints[@]}"; do
  curl --fail --silent --show-error --max-time 5 "$endpoint" >/dev/null
done

llm_hostname="$(python3 - "$ORCHESTRATOR_WORKFLOW_LLM_BASE_URL" <<'PY'
from urllib.parse import urlparse
import sys
print(urlparse(sys.argv[1]).hostname or "")
PY
)"
if [ "$llm_hostname" = "api.deepseek.com" ]; then
  : "${ORCHESTRATOR_WORKFLOW_LLM_API_KEY_FILE:?DeepSeek API key file is required}"
  if [ ! -r "$ORCHESTRATOR_WORKFLOW_LLM_API_KEY_FILE" ]; then
    echo "DeepSeek API key file is not readable: $ORCHESTRATOR_WORKFLOW_LLM_API_KEY_FILE" >&2
    exit 2
  fi
  workflow_llm_api_key="$(tr -d '\r\n' < "$ORCHESTRATOR_WORKFLOW_LLM_API_KEY_FILE")"
  if [ -z "$workflow_llm_api_key" ]; then
    echo "DeepSeek API key file is empty" >&2
    exit 2
  fi
  curl --fail --silent --show-error --max-time 10 \
    --header "Authorization: Bearer $workflow_llm_api_key" \
    "${ORCHESTRATOR_WORKFLOW_LLM_BASE_URL%/}/models" >/dev/null
  unset workflow_llm_api_key
else
  curl --fail --silent --show-error --max-time 5 \
    "${ORCHESTRATOR_WORKFLOW_LLM_BASE_URL%/}/health" >/dev/null
fi
echo "external orchestrator dependencies reachable"

case "${ORCHESTRATOR_V2_ENABLED:-false}" in
  true|TRUE|1|yes|YES)
    capability_url="${N8N_GATEWAY_CAPABILITIES_URL:-$N8N_BASE_URL/api/v2/orchestrator/capabilities}"
    capability_file="$(mktemp)"
    trap 'rm -f -- "$capability_file"' EXIT
    curl --fail --silent --show-error --max-time 5 "$capability_url" >"$capability_file"
    python3 - "$capability_file" <<'PY'
import json
import sys

expected_version = "orchestrator-gateway.v1"
required = {
    "registry_v2",
    "validate",
    "create_inactive",
    "canary",
    "activate",
    "automatic_start",
    "resume",
    "status",
    "retire",
    "live_reference_check",
    "signed_runtime_event_delivery",
    "operation_idempotency",
    "operation_lookup",
}
try:
    document = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"invalid gateway capability document: {error}")
if document.get("schema_version") != "n8n-gateway-capabilities.v1":
    raise SystemExit("gateway capability schema_version is incompatible")
if document.get("api_version") != expected_version:
    raise SystemExit("gateway capability api_version is incompatible")
operations = document.get("operations")
if not isinstance(operations, dict):
    raise SystemExit("gateway capability operations must be an object")
missing = sorted(required - set(operations))
unexpected = sorted(set(operations) - required)
if missing or unexpected:
    raise SystemExit(f"gateway operations mismatch: missing={missing}, unexpected={unexpected}")
for name in sorted(required):
    operation = operations[name]
    if not isinstance(operation, dict):
        raise SystemExit(f"gateway operation {name} must be an object")
    if operation.get("available") is not True:
        raise SystemExit(f"gateway operation is unavailable: {name}")
    if operation.get("api_version") != expected_version:
        raise SystemExit(f"gateway operation has incompatible API version: {name}")
print("n8n gateway capabilities ready")
PY
    ;;
esac
