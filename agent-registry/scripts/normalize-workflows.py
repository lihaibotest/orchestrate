#!/usr/bin/env python3
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "legacy-n8n"
DESTINATION = ROOT / "full-stack" / "workflows"
CREDENTIAL = {
    "id": "agentRegistryPostgres20260819",
    "name": "Agent Registry PostgreSQL",
}
WORKFLOW_IDS = {
    "Agent_Register_Center.json": "agentRegistryRegister20260819",
    "agent-registry-query.json": "agentRegistryQuery20260819",
    "agent-heartbeat.json": "agentRegistryHeartbeat20260819",
    "agent-health-monitor.json": "agentRegistryHealth20260819",
    "agent-registry-maintenance.json": "agentRegistryMaintenance20260819",
}
SECURE_SETTINGS = {
    "executionOrder": "v1",
    "saveDataErrorExecution": "none",
    "saveDataSuccessExecution": "none",
    "saveManualExecutions": False,
    "saveExecutionProgress": False,
}


REGISTER_CODE = r"""const input = $json.body ?? $json;
const agentId = String(input.agent_id ?? '').trim();
const allowed = {
  agent_filter_library: { endpoint: 'http://filter-library-api:8000/api/cases/query', health_url: 'http://filter-library-api:8000/health' },
  agent_wordagent: { endpoint: 'http://wordagent:4324/generate/json', health_url: 'http://wordagent:4324/health' },
  agent_pytestproject: { endpoint: 'http://pytestproject:5000/api/pytest/run', health_url: 'http://pytestproject:5000/api/health' },
  agent_reportagent: { endpoint: 'http://reportagent:6000/api/generate', health_url: 'http://reportagent:6000/api/health' },
};
const expected = allowed[agentId];
if (!expected || String(input.endpoint ?? '') !== expected.endpoint || String(input.health_url ?? '') !== expected.health_url) throw new Error('agent endpoint or health_url is not allowlisted');
const agentName = String(input.agent_name ?? '').trim();
const agentType = String(input.agent_type ?? '').trim();
const capabilities = [...new Set((Array.isArray(input.capabilities) ? input.capabilities : []).map(String).map(v => v.trim()).filter(Boolean))];
if (!agentName || !agentType || capabilities.length === 0) throw new Error('agent_name, agent_type and capabilities are required');
const logPath = input.log_path && typeof input.log_path === 'object' && !Array.isArray(input.log_path) ? input.log_path : {};
const logFile = String(logPath.log_file ?? '').trim();
if (Object.keys(logPath).length > 0 && !logFile) throw new Error('log_path.log_file is required when log_path is provided');
const now = new Date().toISOString();
return [{ json: {
  agent_id: agentId, agent_name: agentName, agent_type: agentType,
  description: String(input.description ?? ''), endpoint: expected.endpoint,
  health_url: expected.health_url, capabilities_json: JSON.stringify({ list: capabilities }),
  intents_json: JSON.stringify(Array.isArray(input.intents) ? input.intents : []),
  metadata_json: JSON.stringify(input.metadata && typeof input.metadata === 'object' && !Array.isArray(input.metadata) ? input.metadata : {}),
  flag: Number.isInteger(Number(input.flag)) ? Number(input.flag) : 0,
  log_path_json: JSON.stringify(logFile ? { ...logPath, log_file: logFile } : {}),
  priority: Number.isInteger(Number(input.priority)) ? Number(input.priority) : 100,
  weight: Number.isInteger(Number(input.weight)) ? Number(input.weight) : 100,
  heartbeat_ttl_seconds: Math.max(30, Number(input.heartbeat_ttl_seconds ?? 300)),
  status: ['active','disabled','pending_verification'].includes(input.status) ? input.status : 'active',
  llm_model: input.llm_model ? String(input.llm_model) : null,
  system_prompt: input.system_prompt ? String(input.system_prompt) : null,
  n8n_workflow_id: input.n8n_workflow_id ? String(input.n8n_workflow_id) : null, now
} }];"""

HEARTBEAT_CODE = r"""const body = $json.body ?? $json;
const agentId = String(body.agent_id ?? '').trim();
if (!agentId) throw new Error('agent_id is required');
return [{json:{agent_id:agentId,status:body.status === 'disabled' ? 'disabled' : 'active',last_error:body.last_error ? String(body.last_error) : null,now:new Date().toISOString()}}];"""

HEALTH_QUERY = """SELECT agent_id, health_url
FROM public.registered_agents
WHERE status IN ('active','stale')
  AND health_url IN (
    'http://filter-library-api:8000/health',
    'http://wordagent:4324/health',
    'http://pytestproject:5000/api/health',
    'http://reportagent:6000/api/health'
  )
ORDER BY priority ASC, agent_id ASC;"""


def node_by_id(workflow, node_id):
    return next(node for node in workflow["nodes"] if node["id"] == node_id)


def normalize(filename):
    workflow = json.loads((SOURCE / filename).read_text(encoding="utf-8"))
    workflow["id"] = WORKFLOW_IDS[filename]
    workflow["settings"] = dict(SECURE_SETTINGS)
    for node in workflow["nodes"]:
        postgres = (node.get("credentials") or {}).get("postgres")
        if postgres is not None:
            node["credentials"]["postgres"] = dict(CREDENTIAL)

    if filename == "Agent_Register_Center.json":
        node_by_id(workflow, "normalize-agent-registration")["parameters"]["jsCode"] = REGISTER_CODE
        upsert = node_by_id(workflow, "upsert-agent-postgres")
        upsert["parameters"]["query"] = (
            "INSERT INTO public.registered_agents (agent_id, agent_name, agent_type, description, endpoint, health_url, llm_model, system_prompt, capabilities, intents, priority, weight, heartbeat_ttl_seconds, metadata, flag, log_path, status, registered_at, last_heartbeat, n8n_workflow_id, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11,$12,$13,$14::jsonb,$15,$16::jsonb,$17,$18::timestamptz,$18::timestamptz,$19,$18::timestamptz) "
            "ON CONFLICT (agent_id) DO UPDATE SET agent_name=EXCLUDED.agent_name, agent_type=EXCLUDED.agent_type, description=EXCLUDED.description, endpoint=EXCLUDED.endpoint, health_url=EXCLUDED.health_url, llm_model=EXCLUDED.llm_model, system_prompt=EXCLUDED.system_prompt, capabilities=EXCLUDED.capabilities, intents=EXCLUDED.intents, priority=EXCLUDED.priority, weight=EXCLUDED.weight, heartbeat_ttl_seconds=EXCLUDED.heartbeat_ttl_seconds, metadata=EXCLUDED.metadata, flag=EXCLUDED.flag, log_path=EXCLUDED.log_path, status=EXCLUDED.status, last_heartbeat=EXCLUDED.last_heartbeat, n8n_workflow_id=EXCLUDED.n8n_workflow_id, updated_at=EXCLUDED.updated_at "
            "RETURNING agent_id, agent_name, agent_type, endpoint, health_url, capabilities, intents, priority, weight, heartbeat_ttl_seconds, metadata, flag, log_path, status, registered_at, last_heartbeat, updated_at;"
        )
        upsert["parameters"]["options"]["queryReplacement"] = (
            "={{ [$json.agent_id,$json.agent_name,$json.agent_type,$json.description,$json.endpoint,$json.health_url,$json.llm_model,$json.system_prompt,$json.capabilities_json,$json.intents_json,$json.priority,$json.weight,$json.heartbeat_ttl_seconds,$json.metadata_json,$json.flag,$json.log_path_json,$json.status,$json.now,$json.n8n_workflow_id] }}"
        )
    elif filename == "agent-heartbeat.json":
        node_by_id(workflow, "normalize-heartbeat")["parameters"]["jsCode"] = HEARTBEAT_CODE
        update = node_by_id(workflow, "update-agent-heartbeat")
        update["parameters"]["query"] = (
            "UPDATE public.registered_agents SET status=$2, last_heartbeat=$3::timestamptz, "
            "updated_at=$3::timestamptz, expires_at=CASE WHEN $2='active' THEN "
            "GREATEST(expires_at,$3::timestamptz + INTERVAL '30 days') ELSE expires_at END, "
            "failure_count=CASE WHEN $2='active' THEN 0 ELSE failure_count+1 END, last_error=$4 "
            "WHERE agent_id=$1 RETURNING agent_id, agent_name, agent_type, "
            "status, endpoint, last_heartbeat, expires_at, updated_at;"
        )
        update["parameters"]["options"]["queryReplacement"] = (
            "={{ [$json.agent_id,$json.status,$json.now,$json.last_error] }}"
        )
    elif filename == "agent-registry-query.json":
        workflow["nodes"] = [
            node for node in workflow["nodes"]
            if node["id"] != "authenticate-registry-query"
        ]
        workflow["connections"].pop("Authenticate Registry Query", None)
        workflow["connections"]["Agent Registry Query Webhook"] = {
            "main": [[{"node": "Select Active Registered Agents", "type": "main", "index": 0}]]
        }
    elif filename == "agent-health-monitor.json":
        node_by_id(workflow, "select-healthcheck-agents")["parameters"]["query"] = HEALTH_QUERY
    return workflow


def main():
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for filename in WORKFLOW_IDS:
        output = DESTINATION / filename
        output.write_text(
            json.dumps(normalize(filename), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
