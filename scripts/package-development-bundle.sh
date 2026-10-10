#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT_DIR="${1:-$PACKAGE_DIR/../dist}"
VERSION="1.1.0"
BASENAME="orchestrator-agent-standalone-dev-$VERSION"
ARCHIVE="$OUTPUT_DIR/$BASENAME.tar.gz"

test -f "$PACKAGE_DIR/artifacts/SHA256SUMS"
test -f "$PACKAGE_DIR/artifacts/images/orchestrator-agent-1.1.0.tar"
"$PACKAGE_DIR/scripts/verify-package.sh"

mkdir -p "$OUTPUT_DIR"
paths=(backend scripts schemas docs docker-compose.yml .dockerignore .env.example README.md manifest.json source-map.json artifacts)
for path in "${paths[@]}"; do
  test -e "$PACKAGE_DIR/$path" || { echo "required bundle path is missing: $path" >&2; exit 1; }
done
tar -C "$PACKAGE_DIR" \
  --transform "s#^#$BASENAME/#" \
  --exclude='*/__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.sqlite3' \
  -czf "$ARCHIVE" "${paths[@]}"
signature_suffix="sig"
if tar -tzf "$ARCHIVE" | grep -E "(^|/)(__pycache__|\\.pytest_cache|tests)(/|$)|\\.pyc$|\\.sqlite3$|\\.pem$|\\.${signature_suffix}$" >/dev/null; then
  echo "forbidden content in development bundle archive" >&2
  exit 1
fi
(cd "$OUTPUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
echo "$ARCHIVE"
