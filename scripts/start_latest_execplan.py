#!/usr/bin/env python3
"""Replay the newest durable ExecPlan through the execution orchestrator.

The task-orchestration service is the only writer of an ExecPlan. This utility
reads the most recent immutable plan from the execution-orchestrator database
and sends that exact payload to the supported submission API. The existing
idempotency key prevents a duplicate PlanVersion while allowing an interrupted
generation to continue.

It deliberately requires an explicit --start and --inputs-file before creating
a formal execution, because runtime inputs are not part of an ExecPlan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = PROJECT_ROOT / ".runtime-secrets" / "execution-submit-token"
SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LaunchError(RuntimeError):
    """Raised for a recoverable operator-facing launch failure."""


@dataclass(frozen=True)
class StoredExecPlan:
    blueprint_id: str
    tenant_id: str
    plan_id: str
    plan_version: int
    idempotency_key: str
    payload: dict[str, Any]
    workflow_family_id: str | None = None
    workflow_version: int | None = None
    generation_status: str | None = None


def json_object(value: object, *, name: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise LaunchError(f"stored {name} is not a JSON object")
    return dict(value)


def load_latest_plan(
    dsn: str,
    *,
    schema: str,
    plan_id: str | None,
    plan_version: int | None,
) -> StoredExecPlan:
    """Read one immutable plan without mutating the task-orchestrator record."""
    if not SCHEMA_NAME.fullmatch(schema):
        raise LaunchError("database schema must be a simple PostgreSQL identifier")
    try:
        import psycopg2  # type: ignore[import-not-found]
        from psycopg2 import sql  # type: ignore[import-not-found]
    except ImportError as error:
        raise LaunchError(
            "psycopg2 is required; run this with the execution service Python environment "
            "or install psycopg2-binary"
        ) from error

    predicates: list[object] = []
    parameters: list[object] = []
    if plan_id:
        predicates.append(sql.SQL("inbound.plan_id=%s"))
        parameters.append(plan_id)
    if plan_version is not None:
        predicates.append(sql.SQL("inbound.plan_version=%s"))
        parameters.append(plan_version)
    where = sql.SQL("")
    if predicates:
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(predicates)
    query = sql.SQL(
        "SELECT inbound.blueprint_id,inbound.tenant_id,inbound.plan_id,inbound.plan_version,"
        "inbound.idempotency_key,inbound.payload_json,generation.workflow_family_id,"
        "generation.workflow_version,generation.status "
        "FROM {}.planning_blueprint_ingestion AS inbound "
        "LEFT JOIN {}.planning_blueprint_generation AS generation "
        "ON generation.blueprint_id=inbound.blueprint_id{} "
        "ORDER BY received_at DESC, plan_version DESC LIMIT 1"
    ).format(sql.Identifier(schema), sql.Identifier(schema), where)

    try:
        with psycopg2.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, parameters)
                row = cursor.fetchone()
    except Exception as error:
        raise LaunchError(f"could not read the execution-plan database: {error}") from error
    if row is None:
        selector = "the requested plan" if plan_id else "any ExecPlan"
        raise LaunchError(f"database contains no {selector}")
    payload = json_object(row[5], name="payload_json")
    return StoredExecPlan(
        blueprint_id=str(row[0]),
        tenant_id=str(row[1]),
        plan_id=str(row[2]),
        plan_version=int(row[3]),
        idempotency_key=str(row[4]),
        payload=payload,
        workflow_family_id=None if row[6] is None else str(row[6]),
        workflow_version=None if row[7] is None else int(row[7]),
        generation_status=None if row[8] is None else str(row[8]),
    )


def post_json(url: str, payload: Mapping[str, object], headers: Mapping[str, str]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:  # nosec B310: operator-configured local endpoint
            raw = response.read().decode("utf-8")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:2000]
        raise LaunchError(f"POST {url} returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise LaunchError(f"could not reach {url}: {error.reason}") from error
    try:
        return json_object(raw, name="API response")
    except json.JSONDecodeError as error:
        raise LaunchError(f"POST {url} returned invalid JSON") from error


def submission_headers(plan: StoredExecPlan, token_file: Path | None) -> dict[str, str]:
    headers = {
        "X-Tenant-Id": plan.tenant_id,
        "Idempotency-Key": plan.idempotency_key,
    }
    if token_file is not None:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise LaunchError(f"could not read execution submit token file: {token_file}") from error
        if not token:
            raise LaunchError("execution submit token file is empty")
        headers["Authorization"] = f"Bearer {token}"
    return headers


def published_reference(response: Mapping[str, object]) -> tuple[str, int]:
    reference = response.get("workflow_reference")
    if not isinstance(reference, Mapping) or reference.get("stage") != "PUBLISHED":
        raise LaunchError(
            "ExecPlan was accepted but is not published; inspect the response for validation, "
            "self-test, or publish failure details"
        )
    family = response.get("workflow_family_id")
    version = response.get("workflow_version")
    if not isinstance(family, str) or not family or not isinstance(version, int) or version < 1:
        raise LaunchError("published ExecPlan response has no valid workflow identity")
    return family, version


def existing_published_reference(plan: StoredExecPlan) -> tuple[str, int] | None:
    """Return a frozen published generation without re-adapting stored JSON.

    Persisted payload JSON is a canonical record, while the inbound wire hash
    preserves the original task-list ordering. Re-submitting a stored payload
    can therefore be rejected correctly by idempotency validation. A published
    generation already has the immutable identity required for execution.
    """
    if plan.generation_status != "PUBLISHED":
        return None
    if not plan.workflow_family_id or plan.workflow_version is None or plan.workflow_version < 1:
        raise LaunchError("published generation has no valid workflow identity")
    return plan.workflow_family_id, plan.workflow_version


def load_inputs(path: Path) -> dict[str, Any]:
    try:
        return json_object(path.read_text(encoding="utf-8"), name="inputs file")
    except OSError as error:
        raise LaunchError(f"could not read inputs file: {path}") from error


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read the latest durable ExecPlan, replay generation, and optionally start formal execution."
    )
    parser.add_argument("--db-dsn", default=os.getenv("ORCHESTRATOR_POSTGRES_DSN"), help="PostgreSQL DSN; defaults to ORCHESTRATOR_POSTGRES_DSN")
    parser.add_argument("--db-schema", default=os.getenv("ORCHESTRATOR_POSTGRES_SCHEMA", "orchestrator"))
    parser.add_argument("--endpoint", default=os.getenv("ORCHESTRATOR_EXECUTION_ENDPOINT", "http://127.0.0.1:28020"))
    parser.add_argument("--plan-id", help="Select a particular immutable plan instead of the newest plan")
    parser.add_argument("--plan-version", type=int, help="Plan version; requires --plan-id")
    parser.add_argument("--submit-token-file", type=Path, default=None, help="Bearer token file for /api/execution/v1/runs")
    parser.add_argument("--start", action="store_true", help="After PUBLISHED, create the formal execution")
    parser.add_argument("--inputs-file", type=Path, help="JSON runtime inputs; required with --start")
    parser.add_argument("--execution-idempotency-key", help="Stable key for formal execution; defaults to plan_id:vN:execute")
    parser.add_argument("--dry-run", action="store_true", help="Print selected plan identity without calling the API")
    args = parser.parse_args(argv)
    if not args.db_dsn:
        parser.error("--db-dsn or ORCHESTRATOR_POSTGRES_DSN is required")
    if args.plan_version is not None and not args.plan_id:
        parser.error("--plan-version requires --plan-id")
    if args.start and args.inputs_file is None:
        parser.error("--start requires --inputs-file")
    if args.dry_run and args.start:
        parser.error("--dry-run cannot be combined with --start")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        plan = load_latest_plan(
            args.db_dsn,
            schema=args.db_schema,
            plan_id=args.plan_id,
            plan_version=args.plan_version,
        )
        identity = {
            "blueprint_id": plan.blueprint_id,
            "tenant_id": plan.tenant_id,
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "idempotency_key": plan.idempotency_key,
            "generation_status": plan.generation_status,
        }
        if args.dry_run:
            print(json.dumps({"selected": identity}, ensure_ascii=True))
            return 0

        token_file = args.submit_token_file
        if token_file is None and DEFAULT_TOKEN_FILE.is_file():
            token_file = DEFAULT_TOKEN_FILE
        endpoint = args.endpoint.rstrip("/")
        existing = existing_published_reference(plan)
        if existing is not None:
            family, version = existing
            submission: dict[str, object] = {
                "status": "PUBLISHED",
                "message": "using existing immutable published generation",
                "workflow_family_id": family,
                "workflow_version": version,
            }
        else:
            submission = post_json(
                f"{endpoint}/api/execution/v1/runs",
                {"exec_plan": plan.payload},
                submission_headers(plan, token_file),
            )
            family, version = published_reference(submission)
        result: dict[str, object] = {
            "selected": identity,
            "submission": submission,
            "workflow_family_id": family,
            "workflow_version": version,
        }
        if args.start:
            execution_key = args.execution_idempotency_key or f"{plan.plan_id}:v{plan.plan_version}:execute"
            execution = post_json(
                f"{endpoint}/api/v1/executions",
                {
                    "workflow_family_id": family,
                    "workflow_version": version,
                    "client_request_id": execution_key,
                    "inputs": load_inputs(args.inputs_file),
                },
                {
                    "X-Tenant-Id": plan.tenant_id,
                    "Idempotency-Key": execution_key,
                },
            )
            result["execution"] = execution
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    except LaunchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
