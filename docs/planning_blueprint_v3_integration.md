# planning-blueprint.v3 入站适配

## 范围

本适配器接收外部 `planning-blueprint.v3` JSON，并转换为内部不可变
`PlanningBlueprintPlan`。外部协议不会成为 Domain 或 Execution IR 的类型；后续
通过 `PlanningBlueprintGenerationPort` 进入已有的 Snapshot、Execution IR、Validator、
Self-Test、Publish 和 Formal Execute 边界。

当前支持 `tasks`、`depends_on`、`capabilities`、`inputs`、`outputs`、
`completion_criteria`、`retry_policy`、`execution_policy` 和 `constraints`。
同时兼容实际上游的 `contract_version`、`goal`、`intent_*`、`timeout_policy`，以及
task 级 `input_bindings`、list-shaped `outputs/completion_criteria`、`iteration` 和
`side_effect`。
只支持 `WHOLE_ARTIFACT` 语义；不支持 `FIELD_MAPPING`。

## 映射规则

- `task_id` 原样保留，作为内部 IR node identity。
- `capabilities` 只用于能力解析；外部不得指定 agent、endpoint、queue、worker、credential 或 n8n node type。
- 物理 Agent 身份、版本、endpoint、I/O schema、credential_ref 和 invocation policy 只能来自冻结 Registry Snapshot。
- `completion_criteria` 保持 JSON object，不降级为文本。
- 输出只保存 artifact expectation metadata，不执行 MinIO HEAD、正文写入或权限校验。
- `input_bindings` 必须使用 `WHOLE_ARTIFACT`，且 source task/output 必须真实存在；Adapter 不猜测错误引用。
- `retry_policy` 只映射为 IR retry metadata；实际技术 retry 仍由 P0-08 ExecutionController/WorkflowEngine Adapter 负责。

## 版本与身份

`plan_id/plan_version` 是上游计划身份，`workflow_family_id/workflow_version` 是内部
生成生命周期。二者不相等也不互相覆盖。`correlation_id`、`session_id`、
`idempotency_key` 在 inbound record、生成引用及后续 Execution lineage 中保留。

## API 与错误

入口为 `POST /api/v1/planning-blueprints`，请求头使用 `X-Tenant-Id`、可选
`X-Correlation-Id` 和 `Idempotency-Key`。同租户同幂等键同内容返回 replay；内容不同返回
`IDEMPOTENCY_CONFLICT`。主要结构化错误包括 `BLUEPRINT_SCHEMA_INVALID`、
`PLAN_DAG_INVALID`、`CAPABILITY_INVALID`、`SECRET_PRESENT` 和
`IDEMPOTENCY_CONFLICT`。未知字段、循环 DAG、悬空依赖、物理 Agent 字段和明文 Secret
均在 Adapter 边界拒绝。

## 持久化

迁移 `0006_planning_blueprint_ingestion` 保存清理后的 inbound metadata、canonical hash、
校验状态和生成引用。租户+幂等键唯一；身份和 payload 通过数据库 trigger 保持不可变。
原始 Secret 不写入 PostgreSQL。

## V3.2 适配性验证

| V3.2 设计要求 | 当前实现 |
|---|---|
| Plan Intake 与核心 IR 解耦 | `PlanningBlueprintV3Adapter` → `PlanningBlueprintPlan` → `build_execution_ir` |
| Registry Snapshot 为物理 Agent 真相 | 仅从 Snapshot 创建 IR binding |
| plan version 与 workflow version 分离 | `requested_workflow_version` 与生成结果独立 |
| correlation / tenant / idempotency lineage | correlation 保留；tenant 通过 Auth/Tenant seam；幂等按 tenant 作用域 |
| Artifact 仅引用与元数据 | 已支持 expectation metadata，MinIO 留 P0-09 |
| Runtime Event / reconciliation | P0-10 已实现：事件接收、Observation、Reconciliation；P1 recovery 仍未实现 |
| FIELD_MAPPING | 不支持，明确拒绝 |

工程内 source_docs 使用 `执行编排智能体总体设计方案_V3.2.docx`；工作区提供的
`执行编排智能体总体设计方案_V3.2_格式规范版.docx` 也已用于接口核对。

## 组合与未完成边界

默认 PostgreSQL 组合根挂载 inbound router，并注入
`ProductionPlanningBlueprintGenerationService`。完整链路为 v3 → internal plan → Agent
Preparation/Snapshot → IR/Validator → n8n Compiler → S0-S4 → Publish → Formal Execute。
generation lineage 持久化 plan/workflow/snapshot/hash/self-test/首个 execution reference；重复提交
和 response-loss replay 不创建 Workflow V2。真实 n8n 联调仍为 TBD-04；当前不实现 Artifact
Manager、Runtime Reconciliation 或认证系统（TBD-08）。

## 实际上游文件调试

`task-agent-current-execplan.json` 的字段形态已被 Adapter 接受，但原内容不能生成 Workflow：
`word-generate` 依赖不存在的 task_id `case-execute`，并引用不存在的
`collect-api-info:api-spec`。原样输入与仅修复依赖分别返回 `PLAN_DAG_INVALID`。在不修改原文件、
仅对内存副本把两处引用指向 `generate-test-case:test-cases` 后，production composition
成功生成并发布 Workflow V1，证明剩余链路可用。
