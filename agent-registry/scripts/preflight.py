#!/usr/bin/env python3
import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
SOURCE_ROOT_ENV = "SOURCE_PLATFORM_ROOT"


def assert_ports_available(ports, host_bind="127.0.0.1"):
    for port in ports:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host_bind, int(port)))
        except OSError as exc:
            raise RuntimeError(f"port already in use: {port}") from exc
        finally:
            listener.close()


def assert_paths_outside_source(source_root, paths):
    if not source_root:
        return
    root = Path(source_root).resolve()
    for path in paths:
        candidate = Path(path).resolve()
        if candidate == root or root in candidate.parents:
            raise RuntimeError(f"path is inside source platform: {candidate}")


def assert_required_files(paths):
    for path in paths:
        if not Path(path).is_file():
            raise RuntimeError(f"required file missing: {path}")


def verify_sha256_manifest(manifest_path):
    manifest = Path(manifest_path)
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, filename = line.split(None, 1)
        filename = filename.lstrip("*")
        artifact = manifest.parent / filename
        if not artifact.is_file():
            raise RuntimeError(f"checksum artifact missing: {artifact}")
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual != digest:
            raise RuntimeError(f"checksum mismatch: {artifact}")


def assert_docker_available():
    for command in (("docker", "info"), ("docker", "compose", "version")):
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Docker prerequisite failed: {' '.join(command)}") from exc


def assert_disk_space(path=ROOT, minimum_bytes=2 * 1024 * 1024 * 1024):
    usage = shutil.disk_usage(path)
    if usage.free < minimum_bytes:
        raise RuntimeError(f"insufficient disk space: {usage.free} bytes free")


def load_env(path):
    values = {}
    if not path or not Path(path).is_file():
        return values
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def run_preflight(env_path, require_images=False):
    env = load_env(env_path)
    host_bind = env.get(CONTRACT["host_bind_env"], "127.0.0.1")
    ports = [int(env.get(f"{name.upper().replace('-', '_')}_PORT", default)) for name, default in CONTRACT["ports"].items()]
    assert_ports_available(ports, host_bind)
    required = [
        ROOT / "compose.yaml",
        ROOT / "contracts.json",
        ROOT / "registration" / "agents.json",
        ROOT / "postgres" / "init" / "00-create-databases.sh",
        ROOT / "postgres" / "init" / "registered_agents.sql",
    ] + [ROOT / "workflows" / filename for filename in CONTRACT["workflows"]]
    assert_required_files(required)
    assert_paths_outside_source(env.get(SOURCE_ROOT_ENV), [ROOT / "data", ROOT / "logs", ROOT / "images"])
    assert_docker_available()
    assert_disk_space()
    sums = ROOT / "images" / "SHA256SUMS"
    if require_images:
        assert_required_files([sums])
        verify_sha256_manifest(sums)
    elif sums.exists():
        verify_sha256_manifest(sums)
    return {"host_bind": host_bind, "ports": ports, "required_files": len(required), "images_checked": sums.exists()}


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only full-stack preflight")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--require-images", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run_preflight(args.env, require_images=args.require_images)
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
