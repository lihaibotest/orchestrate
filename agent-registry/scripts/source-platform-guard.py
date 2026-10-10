#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_SOURCE_ROOT = None
FILTER_HEALTH_URL = "http://127.0.0.1:18482/health"
CONTAINER_COMPARE_FIELDS = (
    "id",
    "name",
    "image_id",
    "state",
    "ports",
    "working_dir",
    "project",
)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guarded_files(source_root):
    root = Path(source_root)
    candidates = [root / "docker-compose.yml", root / "start.sh"]
    workflow_roots = [
        root / "sources" / "orchestrator-agent" / "workflows",
        root / "legacy-n8n",
    ]
    for workflow_root in workflow_roots:
        if workflow_root.is_dir():
            candidates.extend(path for path in workflow_root.rglob("*") if path.is_file())
    return sorted({path.resolve() for path in candidates if path.is_file()})


def docker_container_rows():
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--no-trunc"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not ids:
        return []
    documents = json.loads(
        subprocess.run(
            ["docker", "inspect", *ids],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    rows = []
    for item in documents:
        labels = item.get("Config", {}).get("Labels") or {}
        bindings = item.get("HostConfig", {}).get("PortBindings") or {}
        ports = []
        for container_port, host_bindings in sorted(bindings.items()):
            for binding in host_bindings or []:
                ports.append(
                    f"{binding.get('HostIp', '')}:{binding.get('HostPort', '')}->{container_port}"
                )
        rows.append(
            {
                "id": item.get("Id", ""),
                "name": item.get("Name", "").lstrip("/"),
                "image_id": item.get("Image", ""),
                "state": (item.get("State") or {}).get("Status", ""),
                "status": (item.get("State") or {}).get("Status", ""),
                "ports": ",".join(ports),
                "working_dir": labels.get("com.docker.compose.project.working_dir", ""),
                "project": labels.get("com.docker.compose.project", ""),
            }
        )
    return rows


def is_source_container(row, source_root):
    root = Path(source_root).resolve()
    working_dir = str(row.get("working_dir") or "").strip()
    if not working_dir:
        return False
    try:
        return Path(working_dir).resolve() == root
    except OSError:
        return working_dir == str(source_root)


def default_health_probe(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {"available": True, "status": response.status, "body": body}
    except (OSError, urllib.error.URLError) as exc:
        return {"available": False, "status": None, "body": "", "error": str(exc)}


def published_ports(container_rows):
    ports = set()
    for row in container_rows:
        for match in re.finditer(r":([0-9]+)->", row.get("ports", "")):
            ports.add(int(match.group(1)))
    return sorted(ports)


def capture_baseline(source_root, container_rows=None, health_probe=None):
    if source_root is None:
        raise ValueError("--source-root or SOURCE_PLATFORM_ROOT is required for capture")
    root = Path(source_root).resolve()
    rows = docker_container_rows() if container_rows is None else container_rows
    source_rows = sorted(
        (dict(row) for row in rows if is_source_container(row, root)),
        key=lambda row: (row.get("name", ""), row.get("id", "")),
    )
    probe = health_probe or default_health_probe
    return {
        "protected_source_id": root.name,
        "protected_source_markers": ["docker-compose.yml", "start.sh"],
        "source_content_sha256": sha256_file(root / "docker-compose.yml") if (root / "docker-compose.yml").is_file() else "",
        "files": {
            str(path.relative_to(root)): sha256_file(path) for path in guarded_files(root)
        },
        "containers": source_rows,
        "published_ports": published_ports(source_rows),
        "health": probe(FILTER_HEALTH_URL),
    }


def normalized_containers(rows):
    normalized = []
    for row in rows:
        normalized.append({field: row.get(field, "") for field in CONTAINER_COMPARE_FIELDS})
    return sorted(normalized, key=lambda row: (row["name"], row["id"]))


def compare_baselines(baseline, current):
    errors = []
    if baseline.get("protected_source_id") != current.get("protected_source_id"):
        errors.append("protected source identity changed")
    if baseline.get("files") != current.get("files"):
        errors.append("guarded file hashes changed")
    if normalized_containers(baseline.get("containers", [])) != normalized_containers(
        current.get("containers", [])
    ):
        errors.append("containers changed")
    if baseline.get("published_ports") != current.get("published_ports"):
        errors.append("published ports changed")
    if baseline.get("health") != current.get("health"):
        errors.append("Filter health changed")
    return errors


def assert_paths_outside_source(source_root, paths):
    if source_root is None:
        return
    root = Path(source_root).resolve()
    for path in paths:
        candidate = Path(path).resolve()
        if candidate == root or root in candidate.parents:
            raise ValueError(f"writable path is inside source platform: {candidate}")


def write_json(path, payload, source_root):
    destination = Path(path).resolve()
    assert_paths_outside_source(source_root, [destination])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_parser():
    parser = argparse.ArgumentParser(description="Read-only source platform guard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--source-root", default=None)
    capture.add_argument("--output", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--source-root", default=None)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--output")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    source_root_value = args.source_root or os.getenv("SOURCE_PLATFORM_ROOT")
    source_root = Path(source_root_value) if source_root_value else None
    if args.command == "capture":
        payload = capture_baseline(source_root)
        write_json(args.output, payload, source_root)
        return 0

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = capture_baseline(source_root)
    errors = compare_baselines(baseline, current)
    if args.output:
        write_json(args.output, current, source_root)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("PASS: source platform unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
