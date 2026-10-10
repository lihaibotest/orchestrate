#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
REGISTRY_DIR="${1:?usage: $0 REGISTRY_PROJECT IMAGE_BUILD_PROJECT [OUTPUT_DIR]}"
IMAGE_BUILD_DIR="${2:?usage: $0 REGISTRY_PROJECT IMAGE_BUILD_PROJECT [OUTPUT_DIR]}"
OUTPUT_DIR="${3:-$ROOT_DIR/dist}"
STAGE_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/portable-migration-full.XXXXXX")"
STAGE="$STAGE_PARENT/portable-migration"
ARCHIVE="$OUTPUT_DIR/portable-migration-full.tar.gz"
trap 'rm -rf "$STAGE_PARENT"' EXIT

for path in "$REGISTRY_DIR/compose.yaml" "$REGISTRY_DIR/contracts.json" \
  "$REGISTRY_DIR/registration/agents.json" "$REGISTRY_DIR/postgres/init" \
  "$REGISTRY_DIR/workflows" "$REGISTRY_DIR/scripts"; do
  test -e "$path" || { echo "missing required source: $path" >&2; exit 1; }
done

# The image-build project contains source build contexts on some revisions,
# while the Registry project contains the exported runtime archives.  Prefer
# the Orchestrator's final, already-verified ten-image inventory and accept the
# other two locations only when they are complete inventories.
IMAGE_SOURCE_DIR="$ROOT_DIR/images"
for candidate in "$IMAGE_BUILD_DIR/images" "$REGISTRY_DIR/images" "$ROOT_DIR/images"; do
  if [ -f "$candidate/manifest.json" ] && [ -f "$candidate/SHA256SUMS" ] && [ -d "$candidate/archives" ] \
    && [ "$(find "$candidate/archives" -maxdepth 1 -type f -name '*.tar' | wc -l)" -ge 10 ]; then
    IMAGE_SOURCE_DIR="$candidate"
    break
  fi
done
test -f "$IMAGE_SOURCE_DIR/manifest.json" || { echo "no complete offline image inventory found" >&2; exit 1; }
test -f "$IMAGE_SOURCE_DIR/SHA256SUMS" || { echo "missing image checksum manifest" >&2; exit 1; }
(cd "$IMAGE_SOURCE_DIR" && sha256sum -c SHA256SUMS >/dev/null)

mkdir -p "$OUTPUT_DIR" "$STAGE"/{agent-registry,orchestrator,services,images,config,scripts,docs,data,state,logs}

# The bundle is assembled from explicit runtime inputs. Mutable databases, logs,
# source provenance, caches, and secrets are intentionally excluded.
cp "$ROOT_DIR/docker-compose.migration.yml" "$STAGE/"
cp "$ROOT_DIR/migration-manifest.json" "$STAGE/"
cp "$ROOT_DIR/config/.env.example" "$STAGE/.env.example"
cp "$ROOT_DIR/manifest.json" "$STAGE/orchestrator/"
cp "$ROOT_DIR/README.md" "$STAGE/orchestrator/"
cp "$ROOT_DIR/docker-compose.yml" "$STAGE/orchestrator/"
cp "$ROOT_DIR/pytest.ini" "$STAGE/orchestrator/" 2>/dev/null || true
cp -a "$ROOT_DIR/backend" "$STAGE/orchestrator/"
cp "$ROOT_DIR/.env.example" "$STAGE/orchestrator/" 2>/dev/null || true
cp -a "$ROOT_DIR/config/." "$STAGE/config/"
cp -a "$ROOT_DIR/services/." "$STAGE/services/"
cp -a "$ROOT_DIR/schemas/." "$STAGE/orchestrator/schemas/"
cp -a "$ROOT_DIR/docs/." "$STAGE/docs/"
cp -a "$ROOT_DIR/scripts/." "$STAGE/scripts/"
if [ -d "$ROOT_DIR/data" ]; then
  find "$ROOT_DIR/data" -mindepth 1 -maxdepth 1 -type d -print0 | while IFS= read -r -d '' data_dir; do
    name="$(basename "$data_dir")"
    case "$name" in source-before|build|.runtime-secrets|logs|state) continue;; esac
    mkdir -p "$STAGE/data/$name"
    (cd "$data_dir" && find . -type f \
      ! -name '*.sqlite3' ! -name '*.log' ! -name '*.pyc' ! -path '*/__pycache__/*' -print0) |
      while IFS= read -r -d '' item; do
        mkdir -p "$STAGE/data/$name/$(dirname "$item")"
        cp "$data_dir/$item" "$STAGE/data/$name/$item"
      done
  done
fi

cp "$REGISTRY_DIR/compose.yaml" "$STAGE/agent-registry/"
cp "$REGISTRY_DIR/contracts.json" "$STAGE/agent-registry/"
cp "$REGISTRY_DIR/.env.example" "$STAGE/agent-registry/" 2>/dev/null || true
cp "$REGISTRY_DIR/pytest.ini" "$STAGE/agent-registry/" 2>/dev/null || true
cp -a "$REGISTRY_DIR/registration" "$STAGE/agent-registry/"
cp -a "$REGISTRY_DIR/postgres" "$STAGE/agent-registry/"
cp -a "$REGISTRY_DIR/workflows" "$STAGE/agent-registry/"
cp -a "$REGISTRY_DIR/scripts" "$STAGE/agent-registry/"
mkdir -p "$STAGE/agent-registry/data" "$STAGE/agent-registry/logs"
for fixture_dir in filter-library wordagent pytestproject reportagent; do
  if [ -d "$REGISTRY_DIR/data/$fixture_dir" ]; then
    mkdir -p "$STAGE/agent-registry/data/$fixture_dir"
    (cd "$REGISTRY_DIR/data/$fixture_dir" && find . -type f \
      ! -name '*.sqlite3' ! -name '*.log' ! -name '*.pyc' ! -path '*/__pycache__/*' -print0) | while IFS= read -r -d '' item; do
        mkdir -p "$STAGE/agent-registry/data/$fixture_dir/$(dirname "$item")"
        cp "$REGISTRY_DIR/data/$fixture_dir/$item" "$STAGE/agent-registry/data/$fixture_dir/$item"
      done
  fi
done

cp -a "$IMAGE_SOURCE_DIR/." "$STAGE/images/"
rm -rf "$STAGE/images/.pytest_cache" "$STAGE/images/__pycache__"

python3 - "$STAGE/migration-manifest.json" "$ROOT_DIR/migration-manifest.json" <<'PY'
import json, sys
target, source = sys.argv[1:]
data = json.load(open(source, encoding="utf-8"))
data["bundle_layout"] = {
    "root": "portable-migration",
    "orchestrator": "orchestrator",
    "agent_registry": "agent-registry",
    "migration_compose": "docker-compose.migration.yml",
    "images": "images",
    "runtime_data": "data",
}
data["components"] = {
    "orchestrator": "orchestrator",
    "agent_registry": "agent-registry",
    "planning_agent": "services/planning-agent",
    "gateway": "services/orchestrator-gateway",
}
data["image_manifest"] = "images/manifest.json"
data["image_checksum_manifest"] = "images/SHA256SUMS"
data["source_policy"] = {
    "protected_source_included": False,
    "runtime_allowlist": True,
    "excluded_patterns": ["source-before.json", "source-allowlists.json", "selected-source-manifest.json", ".env", ".runtime-secrets", "*.sqlite3", "*.pyc", "*.log"],
}
data["artifact_signing"] = False
json.dump(data, open(target, "w", encoding="utf-8"), indent=2, sort_keys=True)
open(target, "a", encoding="utf-8").write("\n")
PY

find "$STAGE" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '*.sqlite3' -o -name '*.log' -o -name '.env' \) -delete
tar -C "$STAGE_PARENT" --sort=name --owner=0 --group=0 --numeric-owner \
  --exclude='.runtime-secrets' --exclude='*/.runtime-secrets' \
  -czf "$ARCHIVE" portable-migration
(cd "$OUTPUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
echo "$ARCHIVE"
