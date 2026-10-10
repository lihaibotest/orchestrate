#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/images}"
ARCHIVE_DIR="$OUTPUT_DIR/archives"
mkdir -p "$ARCHIVE_DIR"
required=(
  agent-registry/postgres:20260819 agent-registry/n8n:20260819
  agent-registry/filter-library-api:20260819 agent-registry/filter-library-ui:20260819
  agent-registry/wordagent:20260819 agent-registry/pytestproject:20260819
  agent-registry/reportagent:20260819 orchestrator-agent-standalone:1.1.0
  portable/planning-agent-stub:1.0.0 portable/orchestrator-gateway:1.0.0
)
for image in "${required[@]}"; do
  docker image inspect "$image" >/dev/null || { echo "missing local image: $image" >&2; exit 1; }
  archive="${image//\//_}"; archive="${archive//:/_}.tar"
  docker save --output "$ARCHIVE_DIR/$archive" "$image"
done
python3 - "$OUTPUT_DIR/manifest.json" "$ARCHIVE_DIR" "${required[@]}" <<'PY'
import json, subprocess, sys
from pathlib import Path
output, archive_dir, *images = sys.argv[1:]
inspected = {}
for item in json.loads(subprocess.check_output(["docker", "image", "inspect", *images], text=True)):
    for tag in item.get("RepoTags") or []:
        inspected[tag] = item
entries = {}
for tag in images:
    item = inspected[tag]
    name = f"{tag.replace('/', '_').replace(':', '_')}.tar"
    entries[tag] = {"archive": f"archives/{name}", "id": item["Id"], "architecture": item["Architecture"], "os": item["Os"], "size": item["Size"]}
Path(output).write_text(json.dumps({"schema_version": "portable-migration-images.v1", "images": entries}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
(cd "$OUTPUT_DIR" && sha256sum archives/*.tar manifest.json > SHA256SUMS)
