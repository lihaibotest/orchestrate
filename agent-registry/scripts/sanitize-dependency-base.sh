#!/usr/bin/env bash
set -euo pipefail

COMPONENT="${1:?component is required}"
WORK_ROOT="${2:?work root is required}"

case "$COMPONENT" in
  wordagent) SOURCE_IMAGE="agent-migration/wordagent:bundle-20260805-113632" ;;
  pytestproject) SOURCE_IMAGE="agent-migration/pytestproject:bundle-20260805-113632" ;;
  reportagent) SOURCE_IMAGE="agent-migration/reportagent:bundle-20260805-113632" ;;
  *) printf 'unsupported component: %s\n' "$COMPONENT" >&2; exit 2 ;;
esac

TARGET_IMAGE="agent-registry/deps-${COMPONENT}:20260819"
mkdir -p "$WORK_ROOT"
TEMP_ROOT="$(mktemp -d "$WORK_ROOT/sanitize-${COMPONENT}.XXXXXX")"
CONTAINER_NAME="agent-registry-sanitize-${COMPONENT}-$$"

cleanup() {
  docker container rm --force "$CONTAINER_NAME" >/dev/null 2>&1 || true
  python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$TEMP_ROOT"
}
trap cleanup EXIT

docker create --name "$CONTAINER_NAME" "$SOURCE_IMAGE" >/dev/null
docker export --output "$TEMP_ROOT/raw.tar" "$CONTAINER_NAME"
mkdir -p "$TEMP_ROOT/rootfs"
tar \
  --exclude='app/wordagent' --exclude='app/pytestproject' \
  --exclude='app/reportagent' --exclude='app/*' --exclude='data/*' \
  --exclude='tmp/*' --exclude='run/*' \
  --exclude='root/*' --exclude='home/*' --exclude='var/log/*' \
  --exclude='var/cache/*' --exclude='var/run/*' \
  -xf "$TEMP_ROOT/raw.tar" -C "$TEMP_ROOT/rootfs"

python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' \
  "$TEMP_ROOT/rootfs/opt/host-agent-envs/shared310/lib/python3.10/site-packages/Crypto/SelfTest"

if find "$TEMP_ROOT/rootfs" -type f \( -name '.env' -o -name 'n8n_registration.json' -o -name 'all_code*.txt' -o -name '*.key' \) -print -quit | grep -q .; then
  printf 'sensitive file remained in sanitized dependency root for %s\n' "$COMPONENT" >&2
  exit 1
fi

tar -C "$TEMP_ROOT/rootfs" -cf "$TEMP_ROOT/clean.tar" .
docker import \
  --change 'ENV LANG=C.UTF-8' \
  --change 'ENV PATH=/opt/host-agent-envs/shared310/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
  "$TEMP_ROOT/clean.tar" "$TARGET_IMAGE" >/dev/null
docker image inspect "$TARGET_IMAGE" --format '{{.Id}}'
