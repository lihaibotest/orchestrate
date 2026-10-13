# P0-12 Acceptance Report

- Conclusion: **P0_BLOCKED**
- Mode: `full`
- Generated: `2026-10-12T02:17:43.892481+00:00`
- Migration: `PASS` (expected latest version 10)
- External E2E: `BLOCKED_EXTERNAL`

## Baseline

- Git branch: `main`
- Git commit: `2f783da297fae0ce48cbc1357fe8bd8ea6c69687`
- Worktree has uncommitted changes: `yes` (preserved as part of this implementation)
- Python: `3.10.20`
- PostgreSQL gate: `PASS`; DSN supplied: `yes`
- n8n/Agent/MinIO/CMM: external execution is only run with the explicitly supplied approved command; otherwise status remains `BLOCKED_EXTERNAL`.

## Real External Boundary Smoke

- n8n `n8n-fresh`: health PASS; isolated workflow validate/import/canary/activate/webhook PASS; PostgreSQL `execution_entity` evidence `id=6,status=success`; workflow retired and n8n health restored.
- Word Agent `/health`: PASS (`127.0.0.1:4324`).
- Report Agent `/api/health`: PASS (`127.0.0.1:6000`).
- MinIO: project `MinioArtifactAdapter` put/stat/get/validate/delete/hash PASS in isolated bucket/prefix; cleanup PASS.
- These probes validate real external boundaries only. They do not prove a real business Agent output flowing through Artifact registration, Runtime reconciliation and CMM delivery, so the conclusion remains `P0_BLOCKED` with `BLOCKED_EXTERNAL`.

## Verification Groups

| Group | Result | Exit | Log |
|---|---|---:|---|
| `diff_check` | PASS | 0 | `/tmp/p0-12-logs/diff_check.log` |
| `p0_02_to_p0_11_regression` | PASS | 0 | `/tmp/p0-12-logs/p0_02_to_p0_11_regression.log` |
| `p0_12_fake_and_postgres` | PASS | 0 | `/tmp/p0-12-logs/p0_12_fake_and_postgres.log` |

## Required Faults

- `FI-01 blueprint_execute_replay` -> `test_blueprint_and_execute_replay_have_one_business_identity`
- `FI-02 publish_execute_response_loss` -> `test_execution_http_retry_reuses_execution_identity`
- `FI-03 runtime_event_duplicate_missing` -> `test_runtime_duplicate_and_missing_event_have_evidence`
- `FI-04 agent_stalled` -> `test_agent_stall_has_retry_and_reconciliation_evidence`
- `FI-05 artifact_hash_mismatch` -> `test_artifact_hash_mismatch_is_not_trusted`
- `FI-06 cmm_disconnect_publisher_crash_recovery` -> `test_cmm_publisher_crash_recovers_without_duplicate_delivery`

## Regression

- The group logs above are the authoritative command output. Offline mode runs the full `tests/` discovery with PostgreSQL variables unset; full mode delegates the historical P0-02~P0-11 gate to `scripts/check-p0-11.sh`.
- PostgreSQL migration evidence is required in full mode and is reported as `BLOCKED_REQUIRED` when no DSN is supplied.

## Fault Evidence

- FI-01/FI-02 prove stable blueprint, workflow, execution and external operation identity under replay/response loss.
- FI-03 proves duplicate event idempotency and explicit missing-event reconciliation evidence.
- FI-04 proves stalled agent handling without manufacturing a successful terminal state.
- FI-05 proves hash mismatch becomes `ARTIFACT_MISMATCH` and is not trusted.
- FI-06 proves CMM outage/crash recovery retains the outbox and delivers one logical event after recovery.

## Happy Path Evidence

```json
{
  "artifact_refs": [
    {
      "artifact_id": "artifact-822906e6723d2405cf2cbe536a9cc8f91f73e34f03ad6732087c330049557408",
      "content_hash": "ce0cccb0ed5e52ca1e64f965841fd9d221ac191a03999494d349740788077ff2",
      "producer_attempt_no": 1,
      "producer_node_id": "select-cases",
      "storage_ref": "artifact-store://artifacts/tenant-a/production/wf-de745131945f165e13a2f874/v1/execution-5cb23ce0bd7a656f4f0772d93562fef4c762251853dd2a02a7cb1cbeb5780488/select-cases/attempt-1/selected-cases/artifact-822906e6723d2405cf2cbe536a9cc8f91f73e34f03ad6732087c330049557408",
      "validation_status": "PASS"
    },
    {
      "artifact_id": "artifact-627760aeb314dafc7bbb6210ec522601cc0f3edf20814300dd107ebb8cc9867a",
      "content_hash": "11c11db30f4b3f6c758722ba3e27eea08145b7d212f6a157451e364b48475280",
      "producer_attempt_no": 1,
      "producer_node_id": "execute-cases",
      "storage_ref": "artifact-store://artifacts/tenant-a/production/wf-de745131945f165e13a2f874/v1/execution-5cb23ce0bd7a656f4f0772d93562fef4c762251853dd2a02a7cb1cbeb5780488/execute-cases/attempt-1/test-cases/artifact-627760aeb314dafc7bbb6210ec522601cc0f3edf20814300dd107ebb8cc9867a",
      "validation_status": "PASS"
    }
  ],
  "attempt_numbers": {
    "execute-cases": 1,
    "select-cases": 1
  },
  "cmm_delivery_count": 5,
  "cmm_event_ids": [
    "context-event-5046dc525577956ab1cdd644aafe384215e8ebd768d41ec35a8ec391745ed676",
    "context-event-12daf126683cdd5e81941697b6923c4f1230f88599296d1d827b3e76157e39d8",
    "context-event-e0585f2b6218c7b507a77b4c11e6a2ef93ba0c6fe2ce792fa0977a847f245c4e",
    "context-event-a4d60cf12108961855405842b03d479d7c4602479c9ffd8fc64641a9c172f20d",
    "context-event-c456bda7348cd15a390ddd8c07f272e4e27b308f4a2457931dea4f3711f7026c"
  ],
  "correlation_id": "corr-v3",
  "evidence_steps": [
    "plan",
    "snapshot",
    "ir",
    "s0",
    "s1",
    "s2",
    "s3",
    "s4",
    "publish",
    "execute",
    "runtime_events",
    "succeeded",
    "artifact_ref",
    "cmm_outbox"
  ],
  "execution_history_terminal": "SUCCEEDED",
  "execution_id": "execution-5cb23ce0bd7a656f4f0772d93562fef4c762251853dd2a02a7cb1cbeb5780488",
  "execution_status": "SUCCEEDED",
  "ir_hash": "c20dbce36f66462cf2e5ffbd81fd8ed465f1ad1a79f3693f728295bddb368241",
  "node_statuses": {
    "execute-cases": "SUCCEEDED",
    "select-cases": "SUCCEEDED"
  },
  "registry_snapshot_id": "registry-snapshot-p0-12",
  "runtime_event_ids": [
    "agent-a-done",
    "n8n-a-done",
    "artifact-a-valid",
    "agent-b-done",
    "n8n-b-done",
    "artifact-b-valid",
    "workflow-done"
  ],
  "status": "PASS",
  "tenant_id": "tenant-a",
  "workflow_family_id": "wf-de745131945f165e13a2f874",
  "workflow_hash": "aa51575273fa50ad40a9f1359c1cce5eacf37b3a57aa56656b39b5e5b06b13f3",
  "workflow_version": 1
}
```

## Boundaries

Real n8n/Agent/MinIO/CMM execution is only reported when an approved external command is supplied via `P0_12_EXTERNAL_E2E_CMD`; otherwise it remains `BLOCKED_EXTERNAL` and cannot be treated as PASS.
P1 Checkpoint/Fingerprint, Evaluation/Reuse, Resume Root and PATCH Version Lineage are not implemented by P0-12.
