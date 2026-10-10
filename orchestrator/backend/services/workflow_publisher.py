from __future__ import annotations

import hashlib
import json


class WorkflowPublisher:
    """Legacy v1 facade retained until durable publication owns these stages."""
    def __init__(self, n8n: object) -> None:
        self.n8n = n8n
        self._history: dict[str, list[dict[str, object]]] = {}
        self._active: dict[str, dict[str, object]] = {}

    def publish(self, session_id: str, workflow_version: int, workflow: dict[str, object], *, canary: bool, canary_result: dict[str, object] | None = None, start_payload: dict[str, object] | None = None) -> dict[str, object]:
        self.n8n.static_validate(workflow)
        inactive_workflow = {**workflow, "active": False}
        created = self.n8n.create_inactive(inactive_workflow)
        workflow_id = str(created["id"])
        result = canary_result if canary_result is not None else (self.n8n.run_canary(workflow_id, workflow) if canary else {"status": "passed"})
        if result.get("status") != "passed":
            raise RuntimeError("Canary failed; previous workflow remains active")
        self.n8n.activate(workflow_id)
        trigger_result = None
        if start_payload is not None:
            trigger_result = self.n8n.start_workflow(workflow, {**start_payload, "workflow_id": workflow_id})
        published = {"session_id": session_id, "workflow_version": workflow_version, "workflow_id": workflow_id, "active": True, "workflow": workflow}
        self._history.setdefault(session_id, []).append(published)
        self._active[session_id] = published
        return {"workflow_id": workflow_id, "workflow_version": workflow_version, "active": True, "trigger_result": trigger_result}

    def history(self, session_id: str) -> list[dict[str, object]]:
        return list(self._history.get(session_id, []))

    def active_version(self, session_id: str) -> int | None:
        published = self._active.get(session_id)
        if published is None:
            return None
        return int(published["workflow_version"])

    def retire(self, session_id: str, workflow_version: int) -> None:
        published = next(
            (
                item
                for item in self._history.get(session_id, [])
                if item["workflow_version"] == workflow_version
            ),
            None,
        )
        if published is None:
            raise KeyError((session_id, workflow_version))
        workflow_id = str(published["workflow_id"])
        if self.n8n.has_live_references(workflow_id):
            raise RuntimeError("running execution or Wait checkpoint still references workflow")
        self.n8n.retire(workflow_id)
        published["active"] = False


class DurableWorkflowPublisherFacade:
    """Preserve the v1 publisher response while using durable stage intents."""

    def __init__(self, durable_publisher: object) -> None:
        self.durable_publisher = durable_publisher

    def publish(
        self,
        session_id: str,
        workflow_version: int,
        workflow: dict[str, object],
        *,
        canary: bool,
        canary_result: dict[str, object] | None = None,
        start_payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if canary_result is not None and canary_result.get("status") != "passed":
            raise RuntimeError("Canary failed; previous workflow remains active")
        artifact_hash = hashlib.sha256(
            json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        job_id = f"v1:{session_id}:{workflow_version}:{artifact_hash}"
        result = self.durable_publisher.publish(
            job_id=job_id,
            session_id=session_id,
            plan_version=int(workflow.get("plan_version", 1)),
            workflow_version=workflow_version,
            artifact=workflow,
            start_payload=start_payload or {},
            canary=canary and canary_result is None,
        )
        execution_id = str(result.get("execution_id", ""))
        return {
            "workflow_id": result["workflow_id"],
            "workflow_version": workflow_version,
            "active": True,
            "trigger_result": {"executionId": execution_id} if execution_id else None,
        }
