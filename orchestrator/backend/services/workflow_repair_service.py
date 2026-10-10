from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping

from services.workflow_spec_validator import validate_workflow_spec


class WorkflowRepairService:
    def regenerate(
        self,
        spec: dict[str, object],
        *,
        diagnosis: Mapping[str, object],
        directive: str,
        generate: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        if directive not in {"patch", "replan"}:
            raise ValueError("repair requires patch or replan directive")
        request = {
            "mode": directive,
            "workflow_spec": copy.deepcopy(spec),
            "failure_diagnosis": json.loads(json.dumps(dict(diagnosis), sort_keys=True)),
        }
        generated = generate(request)
        if not isinstance(generated, dict) or "nodes" not in generated or generated.get("schema_version") != "workflow-spec.v1":
            raise ValueError("repair response must be a complete WorkflowSpec")
        validate_workflow_spec(generated)
        previous = json.loads(json.dumps(spec, sort_keys=True))
        candidate = json.loads(json.dumps(generated, sort_keys=True))
        for field in ("session_id", "plan_version", "workflow_version"):
            if candidate.get(field) != previous.get(field):
                raise ValueError(f"repair response changed immutable {field}")
        if candidate == previous:
            raise ValueError("repair response has no material change")
        return candidate
