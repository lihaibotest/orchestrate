#!/usr/bin/env python3
"""Reject workstation-specific paths and secrets from a migration runtime tree."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".runtime-secrets", "plans", "tests"}
SKIP_SUFFIXES = {".tar", ".gz", ".sqlite3", ".pyc", ".docx", ".png", ".jpg", ".jpeg"}
PATH_PATTERNS = (
    # Container-internal home paths (for example n8n's /home/node) are not
    # workstation paths and are valid in Compose volume targets.  Host/user
    # paths remain rejected.
    re.compile(r"/home/(?!node(?:/|$))[A-Za-z0-9._-]+(?:/|$)"),
    re.compile(r"/Users/[A-Za-z0-9._-]+(?:/|$)"),
    re.compile(r"172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+"),
    re.compile(r"172\.17\.0\.1"),
    re.compile(r"host\.docker\.internal"),
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY-----)", re.I),
)


def scan(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or path.name == "check-portable-paths.py" or any(part in SKIP_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in {".env", "source-before.json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in PATH_PATTERNS:
            if pattern.search(text):
                violations.append(f"{path.relative_to(root)}: {pattern.pattern}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text) and ".example" not in path.name:
                violations.append(f"{path.relative_to(root)}: secret-like content")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    violations = scan(Path(args.root).resolve())
    for violation in violations:
        print(f"FAIL: {violation}", file=sys.stderr)
    if violations:
        return 1
    print("PASS: portable path and secret scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
