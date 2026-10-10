#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ARTIFACT_DIR="$PACKAGE_DIR/artifacts"
IMAGE_REF="${ORCHESTRATOR_IMAGE_REF:-orchestrator-agent-standalone:1.1.0}"
BASE_IMAGE_ID="sha256:b916478e77e245f18bd6968cb8ae54a2a323aa960b03ddd738f8344d7c4ea32d"
IMAGE_TAR="$ARTIFACT_DIR/images/orchestrator-agent-1.1.0.tar"

"$PACKAGE_DIR/scripts/build-image-offline.sh"
mkdir -p "$ARTIFACT_DIR/images"
docker save --output "$IMAGE_TAR" "$IMAGE_REF"
IMAGE_ID="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')"

python3 - "$ARTIFACT_DIR/image-manifest.json" "$IMAGE_REF" "$IMAGE_ID" "$BASE_IMAGE_ID" <<'PY'
import json, sys
output, image_ref, image_id, base_image_id = sys.argv[1:]
document = {"schema_version": "offline-image-manifest.v1", "images": [{"ref": image_ref, "id": image_id, "base_image_id": base_image_id, "archive": "artifacts/images/orchestrator-agent-1.1.0.tar"}]}
open(output, "w", encoding="utf-8").write(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY

python3 "$PACKAGE_DIR/scripts/generate_offline_sbom.py" \
  "$IMAGE_TAR" "$ARTIFACT_DIR/sbom.spdx.json" "$IMAGE_ID" "$BASE_IMAGE_ID"

python3 - "$PACKAGE_DIR" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
included = []
for prefix in ("backend", "schemas", "scripts", "docs"):
    for path in sorted((root / prefix).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            included.append(path)
for name in ("docker-compose.yml", ".dockerignore", ".env.example", "README.md", "manifest.json"):
    path = root / name
    if path.is_file(): included.append(path)
files = [{"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(set(included))]
(root / "source-map.json").write_text(json.dumps({"schema_version": "source-map.v2", "files": files}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

(
  cd "$PACKAGE_DIR"
  find artifacts -type f ! -name SHA256SUMS -printf '%P\n' | sed 's#^#artifacts/#'
  printf '%s\n' source-map.json manifest.json docker-compose.yml README.md
) | sort -u | while IFS= read -r path; do
  (cd "$PACKAGE_DIR" && sha256sum "$path")
done > "$ARTIFACT_DIR/SHA256SUMS"

"$PACKAGE_DIR/scripts/verify-package.sh"
echo "development artifacts generated for $IMAGE_ID"
