from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "start_latest_execplan.py"
SPEC = importlib.util.spec_from_file_location("start_latest_execplan", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class StartLatestExecPlanTests(unittest.TestCase):
    def test_published_reference_requires_complete_published_identity(self) -> None:
        with self.assertRaises(MODULE.LaunchError):
            MODULE.published_reference({"workflow_reference": {"stage": "COMPILED"}})
        self.assertEqual(
            MODULE.published_reference(
                {
                    "workflow_reference": {"stage": "PUBLISHED"},
                    "workflow_family_id": "wf-1",
                    "workflow_version": 2,
                }
            ),
            ("wf-1", 2),
        )

    def test_submission_headers_do_not_require_a_token_when_no_file_is_supplied(self) -> None:
        plan = MODULE.StoredExecPlan("b", "tenant-a", "plan-a", 2, "plan-a:v2", {})
        self.assertEqual(
            MODULE.submission_headers(plan, None),
            {"X-Tenant-Id": "tenant-a", "Idempotency-Key": "plan-a:v2"},
        )

    def test_main_replays_selected_plan_without_starting_execution(self) -> None:
        plan = MODULE.StoredExecPlan("b", "tenant-a", "plan-a", 2, "plan-a:v2", {"plan_id": "plan-a"})
        response = {
            "workflow_reference": {"stage": "PUBLISHED"},
            "workflow_family_id": "wf-1",
            "workflow_version": 1,
        }
        with patch.object(MODULE, "load_latest_plan", return_value=plan), patch.object(
            MODULE, "post_json", return_value=response
        ) as post, patch.object(MODULE, "DEFAULT_TOKEN_FILE", Path("/missing-default-token")):
            code = MODULE.main(["--db-dsn", "postgresql://example"])
        self.assertEqual(code, 0)
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:28020/api/execution/v1/runs")
        self.assertEqual(post.call_args.args[1], {"exec_plan": {"plan_id": "plan-a"}})
        self.assertEqual(
            post.call_args.args[2],
            {"X-Tenant-Id": "tenant-a", "Idempotency-Key": "plan-a:v2"},
        )

    def test_main_uses_published_generation_without_replaying_stored_payload(self) -> None:
        plan = MODULE.StoredExecPlan(
            "b", "tenant-a", "plan-a", 2, "plan-a:v2", {}, "wf-1", 1, "PUBLISHED"
        )
        with patch.object(MODULE, "load_latest_plan", return_value=plan), patch.object(
            MODULE, "post_json"
        ) as post, patch.object(MODULE, "DEFAULT_TOKEN_FILE", Path("/missing-default-token")):
            code = MODULE.main(["--db-dsn", "postgresql://example"])
        self.assertEqual(code, 0)
        post.assert_not_called()

    def test_main_does_not_ignore_an_explicit_missing_token(self) -> None:
        plan = MODULE.StoredExecPlan("b", "tenant-a", "plan-a", 2, "plan-a:v2", {})
        with patch.object(MODULE, "load_latest_plan", return_value=plan), patch.object(
            MODULE, "post_json"
        ) as post:
            code = MODULE.main(["--db-dsn", "postgresql://example", "--submit-token-file", "/missing-token"])
        self.assertEqual(code, 2)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
