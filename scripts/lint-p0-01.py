#!/usr/bin/env python3
"""Deterministic offline lint gate for the P0-01 foundation."""

from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "orchestrator" / "backend"
FILES = sorted(BACKEND.rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))
errors: list[str] = []

for path in FILES:
    relative = path.relative_to(ROOT)
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(relative))
    except SyntaxError as error:
        errors.append(f"{relative}:{error.lineno}: syntax error: {error.msg}")
        continue
    for number, line in enumerate(source.splitlines(), 1):
        if line.rstrip() != line:
            errors.append(f"{relative}:{number}: trailing whitespace")
        if "\t" in line:
            errors.append(f"{relative}:{number}: tab character")
    if relative.as_posix() == "orchestrator/backend/main.py":
        forbidden_names = {
            "ExternalControlPlane",
            "DurablePublisher",
            "N8nExecutionBackend",
            "PlanningJobRunner",
            "PlanningJobWorker",
            "OutboxDispatcher",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                    errors.append(
                        f"{relative}:{node.lineno}: environment access belongs in Settings"
                    )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in forbidden_names
            ):
                errors.append(
                    f"{relative}:{node.lineno}: {node.func.id} belongs in the composition root"
                )

domain = BACKEND / "domain"
for path in domain.rglob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            if any(
                name.split(".")[0] in {"fastapi", "services", "adapters", "backends"}
                for name in names
            ):
                errors.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: domain layer imports an adapter"
                )

if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
print(f"P0-01 offline lint passed: {len(FILES)} Python files")
