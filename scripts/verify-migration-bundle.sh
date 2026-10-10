#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
test -f "$ROOT_DIR/migration-manifest.json"
test -d "$ROOT_DIR/agent-registry" || { echo "missing bundled Agent Registry" >&2; exit 1; }
test -f "$ROOT_DIR/images/manifest.json" || { echo "missing image manifest" >&2; exit 1; }
test -f "$ROOT_DIR/images/SHA256SUMS" || { echo "missing image checksums" >&2; exit 1; }
(cd "$ROOT_DIR/images" && sha256sum -c SHA256SUMS)
python3 - "$ROOT_DIR/migration-manifest.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1],encoding='utf-8'))
assert data['schema_version']=='portable-migration.v1'
assert data['artifact_signing'] is False
assert data['architectures']
assert data.get('bundle_layout', {}).get('agent_registry') == 'agent-registry'
manifest=json.load(open(sys.argv[1].replace('migration-manifest.json','images/manifest.json'),encoding='utf-8'))
assert len(manifest.get('images', {})) >= 10
for entry in manifest['images'].values():
    assert not entry['archive'].startswith('/')
    assert __import__('os').path.isfile(__import__('os').path.join(__import__('os').path.dirname(sys.argv[1]), 'images', entry['archive']))
PY
python3 "$ROOT_DIR/scripts/check-portable-paths.py" --root "$ROOT_DIR"
echo "migration bundle verification passed"
