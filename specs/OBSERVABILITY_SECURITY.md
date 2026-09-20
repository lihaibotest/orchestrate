# Observability & Security

## Runtime Event 最小关联字段
execution_id, workflow_version, node_id, agent_id, attempt, duration, artifact_refs, checkpoint_ref, trace_id/request_id, error_code, timestamp。

## Metrics
workflow_self_test_success_rate, agent_readiness_rate, node_success_rate, rework_ratio, artifact_reuse_success_rate, execution_stalled_count, cmm_outbox_lag。

## 日志字段
`timestamp level service trace_id correlation_id tenant_id workflow_family_id workflow_version execution_id node_id agent_id attempt event_type error_code duration_ms`

## 安全硬约束
- Credential 只允许 ref；禁止 Secret 明文。
- tenant_id 必须与认证主体校验，不可信任客户端自报。
- MinIO 按 tenant/session/workflow prefix 最小权限。
- 自测凭据、prefix、数据与正式环境隔离。
- 跨任务/会话/租户 Artifact 复用必须再次鉴权。
- 敏感字段日志脱敏。
