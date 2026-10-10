#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
IMAGE_DIR="${1:-$ROOT_DIR/images}"
python3 - "$IMAGE_DIR/manifest.json" "$IMAGE_DIR" <<'PY'
import json, sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert manifest.get("schema_version") == "portable-migration-images.v1"
assert len(manifest.get("images", {})) == 10
root = Path(sys.argv[2]).resolve()
for tag, item in manifest["images"].items():
    archive = (root / item["archive"]).resolve()
    if root not in archive.parents or not archive.is_file():
        raise SystemExit(f"invalid or missing archive for {tag}: {archive}")
PY
(cd "$IMAGE_DIR" && sha256sum -c SHA256SUMS)
while IFS= read -r archive; do docker load --input "$IMAGE_DIR/$archive"; done < <(python3 - "$IMAGE_DIR/manifest.json" <<'PY'
import json, sys
for item in json.load(open(sys.argv[1], encoding="utf-8"))["images"].values(): print(item["archive"])
PY
)
