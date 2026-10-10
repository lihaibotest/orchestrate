#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/dist}"
BASENAME="portable-migration"
mkdir -p "$OUTPUT_DIR"
required=(docker-compose.migration.yml migration-manifest.json manifest.json scripts services schemas docs config images)
for path in "${required[@]}"; do test -e "$ROOT_DIR/$path" || { echo "missing bundle path: $path" >&2; exit 1; }; done
tar -C "$ROOT_DIR" --sort=name --owner=0 --group=0 --numeric-owner --exclude='*.sqlite3' --exclude='*.pyc' --exclude='__pycache__' --exclude='.runtime-secrets' --exclude='*/.runtime-secrets' -czf "$OUTPUT_DIR/$BASENAME.tar.gz" "${required[@]}"
(cd "$OUTPUT_DIR" && sha256sum "$BASENAME.tar.gz" > "$BASENAME.tar.gz.sha256")
echo "$OUTPUT_DIR/$BASENAME.tar.gz"
