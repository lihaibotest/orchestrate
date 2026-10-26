# MinIO Agent artifact namespace

Every compiled Agent invocation carries an execution-scoped object prefix:

```text
ai-agents/{agent}/{plan_id}_{plan_version}_{execution_id}/
```

The compiler emits both `artifact_root` and `artifact_path` with this value,
plus the individual `artifact_namespace`. `execution_id` is the real n8n
execution identity (`$execution.id`); it is deliberately not replaced by
`run_id` or `request_id`.

The deployment registry IDs are normalized for the path:

| registry ID | MinIO agent segment |
| --- | --- |
| `agent_wordagent` | `word_agent` |
| `agent_pytestproject` | `worker` |
| `agent_reportagent` | `report_agent` |

The MinIO adapter accepts the same namespace through `ArtifactPutRequest`.
When it is supplied, objects are written below
`ai-agents/{agent}/{namespace}/{output_name}/{artifact_id}`. Existing callers
that do not provide a namespace retain the legacy storage key for backwards
compatibility.

Agent implementations must use `artifact_root` (or `artifact_path`) as their
upload prefix and return the resulting bucket/object reference in their
response. The orchestration compiler cannot rewrite an object key after an
external Agent has uploaded it.
