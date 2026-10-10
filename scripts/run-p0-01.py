#!/usr/bin/env python3
"""Run the complete P0-01 quality gate with one Python 3.10+ interpreter."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str) -> None:
    subprocess.run(arguments, check=True)


def main() -> int:
    if sys.version_info < (3, 10):
        raise RuntimeError("P0-01 checks require the unchanged Python 3.10 runtime baseline")
    run(sys.executable, str(ROOT / "scripts/lint-p0-01.py"))
    run(sys.executable, str(ROOT / "scripts/typecheck-p0-01.py"))
    if importlib.util.find_spec("ruff") is not None:
        run(
            sys.executable, "-m", "ruff", "check", "--select", "E9,F63,F7,F82",
            str(ROOT / "orchestrator/backend"), str(ROOT / "tests"),
        )
    else:
        print("ruff not installed; deterministic offline lint gate passed", file=sys.stderr)
    if importlib.util.find_spec("mypy") is not None:
        run(
            sys.executable, "-m", "mypy", "--config-file", str(ROOT / "pyproject.toml"),
            str(ROOT / "orchestrator/backend/application"),
            str(ROOT / "orchestrator/backend/infrastructure"),
            str(ROOT / "orchestrator/backend/ports"),
        )
    else:
        print("mypy not installed; deterministic offline type gate passed", file=sys.stderr)
    run(
        sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
        "-p", "test_*.py", "-v",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
