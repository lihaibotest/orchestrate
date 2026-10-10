#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
COMPOSE=(docker compose --project-directory "$ROOT_DIR" -f "$ROOT_DIR/compose.yaml")
N8N_CONTAINER="$("${COMPOSE[@]}" ps -q n8n)"
MANIFEST_PATH="$ROOT_DIR/registration/agents.json"

if [ -z "$N8N_CONTAINER" ]; then
  printf 'n8n container is not running\n' >&2
  exit 1
fi

docker cp "$MANIFEST_PATH" "$N8N_CONTAINER:/tmp/agents.json"
docker exec "$N8N_CONTAINER" node -e "const fs=require('fs'); const agents=JSON.parse(fs.readFileSync('/tmp/agents.json','utf8')); (async()=>{for(const agent of agents){const response=await fetch('http://127.0.0.1:5678/webhook/agent-register',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(agent)}); if(!response.ok){throw new Error(agent.agent_id + ': HTTP ' + response.status)}}})().catch(error=>{console.error(error);process.exit(1)})"
docker exec -u 0 "$N8N_CONTAINER" rm -f /tmp/agents.json
