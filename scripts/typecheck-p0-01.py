#!/usr/bin/env python3
"""Offline structural type gate used when mypy is unavailable."""

from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ROOT / "orchestrator/backend/adapters/fake_agents.py",
    ROOT / "orchestrator/backend/adapters/postgres.py",
    ROOT / "orchestrator/backend/adapters/fake_workflow_engine.py",
    ROOT / "orchestrator/backend/adapters/fake_self_test.py",
    ROOT / "orchestrator/backend/adapters/postgres_self_test.py",
    ROOT / "orchestrator/backend/adapters/fake_execution.py",
    ROOT / "orchestrator/backend/adapters/postgres_execution.py",
    ROOT / "orchestrator/backend/application/agent_preparation_service.py",
    ROOT / "orchestrator/backend/application/container.py",
    ROOT / "orchestrator/backend/application/execution_ir_service.py",
    ROOT / "orchestrator/backend/application/workflow_compilation_service.py",
    ROOT / "orchestrator/backend/application/self_test_controller.py",
    ROOT / "orchestrator/backend/application/self_test_stages.py",
    ROOT / "orchestrator/backend/application/workflow_release_service.py",
    ROOT / "orchestrator/backend/application/state_transition_service.py",
    ROOT / "orchestrator/backend/application/execution_controller.py",
    ROOT / "orchestrator/backend/domain/persistence.py",
    ROOT / "orchestrator/backend/domain/agent_preparation.py",
    ROOT / "orchestrator/backend/domain/execution_ir.py",
    ROOT / "orchestrator/backend/domain/execution_ir_validator.py",
    ROOT / "orchestrator/backend/domain/state_machines.py",
    ROOT / "orchestrator/backend/domain/self_test.py",
    ROOT / "orchestrator/backend/domain/execution_control.py",
    ROOT / "orchestrator/backend/infrastructure/config.py",
    ROOT / "orchestrator/backend/infrastructure/migrations.py",
    ROOT / "orchestrator/backend/infrastructure/observability.py",
    ROOT / "orchestrator/backend/ports/control_plane_repository.py",
    ROOT / "orchestrator/backend/ports/agent_preparation.py",
    ROOT / "orchestrator/backend/ports/external_control.py",
    ROOT / "orchestrator/backend/ports/health.py",
    ROOT / "orchestrator/backend/ports/observability.py",
    ROOT / "orchestrator/backend/ports/repositories.py",
    ROOT / "orchestrator/backend/ports/workflow_engine.py",
    ROOT / "orchestrator/backend/ports/self_test.py",
    ROOT / "orchestrator/backend/ports/execution_control.py",
    ROOT / "orchestrator/backend/api/executions.py",
    ROOT / "orchestrator/backend/backends/n8n/workflow_compiler.py",
    ROOT / "orchestrator/backend/backends/n8n/workflow_engine.py",
]
errors: list[str] = []

for path in FILES:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and node.name not in {"__init__"}:
                continue
            if node.returns is None:
                errors.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: {node.name} has no return annotation"
                )
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for argument in arguments:
                if argument.arg in {"self", "cls"}:
                    continue
                if argument.annotation is None:
                    errors.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: "
                        f"{node.name}.{argument.arg} has no annotation"
                    )

if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
print(f"P0-01 offline type gate passed: {len(FILES)} typed foundation files")
