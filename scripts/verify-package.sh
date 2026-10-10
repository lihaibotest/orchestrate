#!/usr/bin/env bash
set -euo pipefail
PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ARTIFACT_DIR="$PACKAGE_DIR/artifacts"
LOAD_IMAGES=0
SCAN_ROOT=""
if [ "${1:-}" = "--load" ]; then LOAD_IMAGES=1; fi
if [ "${1:-}" = "--scan-root" ]; then SCAN_ROOT="${IMAGE_SCAN_ROOT:?IMAGE_SCAN_ROOT is required}"; fi
(cd "$PACKAGE_DIR" && sha256sum -c artifacts/SHA256SUMS)
first_record="$(head -n 1 "$ARTIFACT_DIR/SHA256SUMS")"
expected_hash="${first_record%% *}"
first_path="${first_record#*  }"
single_byte="$(mktemp)"; cp "$PACKAGE_DIR/$first_path" "$single_byte"; printf X | dd of="$single_byte" bs=1 seek=0 count=1 conv=notrunc status=none
if printf '%s  %s\n' "$expected_hash" "$single_byte" | sha256sum -c - >/dev/null 2>&1; then rm -f "$single_byte"; echo "single-byte tamper was not rejected" >&2; exit 1; fi
rm -f "$single_byte"
scan_listing() {
  local listing="$1"
  if printf '%s\n' "$listing" | tr '[:upper:]' '[:lower:]' | grep -E '(^|/)(_source-reference|node_modules|__pycache__|.*\.pyc$|.*\.sqlite3$|.*\.local\.|(^|/)\.env($|\.)|id_rsa|credentials\.json|.*\.pem$|.*\.key$)' >/dev/null; then
    echo "forbidden or secret-like package content" >&2
    exit 1
  fi
}
scan_package_tree() {
  local listing
  listing="$(cd "$PACKAGE_DIR" && find . -type f -printf '%P\n' | grep -v -E '^\.runtime-secrets(/|$)|(^|/)\.runtime-secrets(/|$)|^\.env$|^\.env.example$')"
  scan_listing "$listing"
  if rg -n -i \
    '(-----BEGIN( [A-Z]+)? PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9-]{10,})' \
    "$PACKAGE_DIR" >/dev/null; then
    echo "secret-like content in package" >&2
    exit 1
  fi
}
scan_portable_paths() {
  if [ -x "$PACKAGE_DIR/scripts/check-portable-paths.py" ] || [ -f "$PACKAGE_DIR/scripts/check-portable-paths.py" ]; then
    python3 "$PACKAGE_DIR/scripts/check-portable-paths.py" --root "$PACKAGE_DIR"
  fi
}
scan_image_root() {
  local root="$1" layer
  if [ -f "$root/manifest.json" ]; then
    while IFS= read -r layer; do
      [ -z "$layer" ] || scan_listing "$(tar -tf "$root/$layer")"
    done < <(python3 - "$root/manifest.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for image in data:
    for layer in image.get("Layers", []):
        print(layer)
PY
)
  else
    while IFS= read -r layer; do scan_listing "$(tar -tf "$layer")"; done < <(find "$root" -name layer.tar -type f)
  fi
}
scan_layer() { local root; root="$(mktemp -d)"; tar -xf "$1" -C "$root"; scan_image_root "$root"; }
if [ -n "$SCAN_ROOT" ]; then scan_image_root "$SCAN_ROOT"; echo "OCI layer scan passed"; exit 0; fi
scan_package_tree
scan_portable_paths
if [ -f "$ARTIFACT_DIR/images/orchestrator-agent-1.1.0.tar" ]; then scan_layer "$ARTIFACT_DIR/images/orchestrator-agent-1.1.0.tar"; fi
if [ "$LOAD_IMAGES" -eq 1 ]; then docker load -i "$ARTIFACT_DIR/images/orchestrator-agent-1.1.0.tar"; fi
echo "orchestrator development package hashes, tamper gate, and layer scan passed"
