#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT_PATH="${1:-$ROOT_DIR/.env}"
OUTPUT_PATH="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$OUTPUT_PATH")"

if [ -e "$OUTPUT_PATH" ]; then
  exit 0
fi

mkdir -p "$(dirname -- "$OUTPUT_PATH")"
cp "$ROOT_DIR/.env.example" "$OUTPUT_PATH"
