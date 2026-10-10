#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
required=(docker tar sha256sum awk sed grep mktemp curl)
missing=()
for command in "${required[@]}"; do command -v "$command" >/dev/null 2>&1 || missing+=("$command"); done
if ((${#missing[@]})); then printf 'missing_commands=%s\n' "${missing[*]}" >&2; exit 1; fi
docker info >/dev/null 2>&1 || { echo 'docker_info=failed' >&2; exit 1; }
docker compose version --short >/dev/null 2>&1 || { echo 'docker_compose_v2=failed' >&2; exit 1; }
host_arch="$(uname -m)"
case "$host_arch" in x86_64|amd64) host_arch=amd64;; aarch64|arm64) host_arch=arm64;; esac
supported="$(sed -n '/"architectures"[[:space:]]*:/,/]/p' "$ROOT_DIR/migration-manifest.json" | grep -oE '"(amd64|arm64)"' | tr -d '"' | tr '\n' ' ' | sed 's/[[:space:]]*$//' )"
[ -n "$supported" ] || supported=amd64
case " $supported " in *" $host_arch "*) ;; *) echo "architecture_mismatch=host:$host_arch supported:$supported" >&2; exit 1;; esac
printf '{"status":"ok","architecture":"%s","supported_architectures":"%s"}\n' "$host_arch" "$supported"
