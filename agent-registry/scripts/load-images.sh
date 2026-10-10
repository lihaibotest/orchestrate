#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
IMAGE_DIR="${1:-$ROOT_DIR/images}"

(
  cd "$IMAGE_DIR"
  sha256sum -c SHA256SUMS
  archives=(archives/*.tar)
  if [ ! -f "${archives[0]}" ]; then
    printf 'no offline image archives found in %s/archives\n' "$IMAGE_DIR" >&2
    exit 1
  fi
  for archive in "${archives[@]}"; do
    docker load --input "$archive"
  done
)
