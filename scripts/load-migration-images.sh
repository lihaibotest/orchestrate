#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
IMAGE_DIR="${MIGRATION_IMAGE_DIR:-$ROOT_DIR/images}"
test -f "$IMAGE_DIR/SHA256SUMS" || { echo "missing migration image checksum manifest" >&2; exit 1; }
(cd "$IMAGE_DIR" && sha256sum -c SHA256SUMS)
while IFS= read -r archive; do docker load --input "$IMAGE_DIR/$archive"; done < <(find "$IMAGE_DIR/archives" -maxdepth 1 -type f -name '*.tar' -printf 'archives/%f\n' | sort)
