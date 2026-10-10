#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SECRETS_DIR="${RUNTIME_SECRETS_DIR:-$ROOT_DIR/.runtime-secrets}"
umask 077
mkdir -p "$SECRETS_DIR"
for name in gateway-api-key planning-hmac n8n-hmac n8n-webhook-path n8n-webhook-auth n8n-api-key; do
  file="$SECRETS_DIR/$name"
  if [ ! -e "$file" ]; then head -c 32 /dev/urandom | base64 | tr -d '\n' > "$file"; chmod 600 "$file"; fi
  [ -r "$file" ] || { echo "secret unreadable: $file" >&2; exit 1; }
done
if [ ! -e "$SECRETS_DIR/deepseek-api-key" ]; then
  echo "provide DeepSeek key in $SECRETS_DIR/deepseek-api-key before starting" >&2
  exit 1
fi
chmod 600 "$SECRETS_DIR"/*
echo "runtime secrets prepared under .runtime-secrets (contents not printed)"
