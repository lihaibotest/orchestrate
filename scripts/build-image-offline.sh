#!/usr/bin/env bash
set -euo pipefail

PACKAGE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
EXPECTED_BASE_IMAGE_ID="sha256:b916478e77e245f18bd6968cb8ae54a2a323aa960b03ddd738f8344d7c4ea32d"
IMAGE_REF="${ORCHESTRATOR_IMAGE_REF:-orchestrator-agent-standalone:1.1.0}"

actual_base_id="$(docker image inspect "$EXPECTED_BASE_IMAGE_ID" --format '{{.Id}}' 2>/dev/null || true)"
if [ "$actual_base_id" != "$EXPECTED_BASE_IMAGE_ID" ]; then
  echo "required immutable base image is not loaded: $EXPECTED_BASE_IMAGE_ID" >&2
  exit 2
fi

# BuildKit resolves a bare image ID as a remote repository. The legacy builder
# is required here so the pinned local content ID remains fully offline.
DOCKER_BUILDKIT=0 docker build \
  --network=none \
  --build-arg "EXPECTED_BASE_IMAGE_ID=$EXPECTED_BASE_IMAGE_ID" \
  --tag "$IMAGE_REF" \
  --file "$PACKAGE_DIR/backend/Dockerfile" \
  "$PACKAGE_DIR"

docker image inspect "$IMAGE_REF" --format '{{.Id}}'
