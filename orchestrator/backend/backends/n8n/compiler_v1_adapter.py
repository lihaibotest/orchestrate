from __future__ import annotations

from services.n8n_workflow_compiler import compile_workflow_spec


class N8nV1CompilerAdapter:
    """BackendCompiler facade around the frozen v1 compiler."""

    def capabilities(self) -> set[str]:
        return {"business_agent", "decision", "wait", "end", "automatic_start", "canary"}

    def validate_ir(self, execution_ir: dict[str, object]) -> list[dict[str, object]]:
        required = {"schema_version", "session_id", "plan_version", "workflow_version", "nodes"}
        missing = sorted(required - set(execution_ir))
        return [
            {"code": "V1_SPEC_FIELD_MISSING", "path": field, "message": f"required field {field} is missing"}
            for field in missing
        ]

    def compile(self, execution_ir: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        errors = self.validate_ir(execution_ir)
        if errors:
            raise ValueError(errors)
        return compile_workflow_spec(
            execution_ir,
            agents=context["agents"],
            webhook_path_secret=str(context.get("webhook_path_secret", "development-webhook-path-secret")),
            webhook_credential_ref=str(context.get("webhook_credential_ref", "orchestrator-webhook")),
        )

