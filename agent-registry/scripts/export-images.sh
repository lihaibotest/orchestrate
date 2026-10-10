#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/images}"
OUTPUT_DIR="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$OUTPUT_DIR")"
ARCHIVE_DIR="$OUTPUT_DIR/archives"
MANIFEST_PATH="$OUTPUT_DIR/manifest.json"
TEMP_MANIFEST="$OUTPUT_DIR/.manifest.json.tmp.$$"
IMAGES=(
  agent-registry/postgres:20260819
  agent-registry/n8n:20260819
  agent-registry/wordagent:20260819
  agent-registry/pytestproject:20260819
  agent-registry/reportagent:20260819
  agent-registry/filter-library-api:20260819
  agent-registry/filter-library-ui:20260819
)

cleanup() {
  rm -f "$TEMP_MANIFEST"
}
trap cleanup EXIT

mkdir -p "$ARCHIVE_DIR"
for image in "${IMAGES[@]}"; do
  docker image inspect "$image" >/dev/null
done

for image in "${IMAGES[@]}"; do
  archive_name="${image//\//_}"
  archive_name="${archive_name//:/_}.tar"
  temp_archive="$ARCHIVE_DIR/.${archive_name}.tmp.$$"
  docker save --output "$temp_archive" "$image"
  sync "$temp_archive"
  mv -f "$temp_archive" "$ARCHIVE_DIR/$archive_name"
done

python3 - "$TEMP_MANIFEST" "${IMAGES[@]}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

output = Path(sys.argv[1])
images = sys.argv[2:]
inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", *images], text=True))
by_tag = {}
for item in inspected:
    for tag in item.get("RepoTags") or []:
        if tag in images:
            by_tag[tag] = {
                "archive": f"archives/{tag.replace('/', '_').replace(':', '_')}.tar",
                "id": item["Id"],
                "architecture": item["Architecture"],
                "os": item["Os"],
                "size": item["Size"],
            }
missing = sorted(set(images) - set(by_tag))
if missing:
    raise SystemExit(f"missing inspected tags: {', '.join(missing)}")
output.write_text(
    json.dumps({"schema_version": 1, "images": by_tag}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
mv -f "$TEMP_MANIFEST" "$MANIFEST_PATH"

(
  cd "$OUTPUT_DIR"
  sha256sum archives/*.tar manifest.json > SHA256SUMS
)
trap - EXIT
