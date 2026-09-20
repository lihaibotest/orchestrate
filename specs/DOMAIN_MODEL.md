# Domain Model

## 聚合/实体
**WorkflowFamily**：同一业务目标的版本族；关联 tenant/task/context。

**WorkflowVersion**：不可变版本。关键字段：family_id, version, parent_version, registry_snapshot_id, ir_hash, structure_ref, status。结构/Binding/Resume Root 变化→新版本。

**RegistrySnapshot**：冻结 agent_id/version/capability/endpoint/I/O schema/credential_ref/call policy；有 snapshot_hash；创建后不可修改。

**Execution**：某 Workflow Version 的正式运行实例；维护 execution status、n8n execution ref、correlation。

**NodeExecution**：逻辑节点在 Execution 中的状态实例。

**Attempt**：业务执行尝试。技术 retry 不增加 attempt_no；业务重新执行增加 attempt_no。

**ArtifactRef**：artifact_id, uri, content_hash, schema, producer, quality, reusable, input_fingerprint, permission_scope。

**Checkpoint**：可信恢复边界，保存 node、artifact refs、input fingerprint、可信状态。

**SelfTestRun**：S0~S4 证据、failure_stage/error_signature/repair_action。

**RuntimeEvent**：归一化运行事实。

**ContextOutboxEvent**：与本地状态事务一致写入，异步幂等 append CMM。

## 关键不变量
1. Published/Verified WorkflowVersion 不可原地修改。
2. Binding 必须来自该版本绑定的 RegistrySnapshot。
3. Secret 明文禁止进入 IR/DB/log。
4. 未满足发布门禁不得创建正式 Execution。
5. Artifact 复用必须重新验证对象存在、hash、schema、input applicability、quality、permission。
6. idempotency key 对同一业务副作用稳定。
7. CMM 写失败不能回滚已经发生的外部运行事实；使用 Outbox 最终一致。
