# Implementation Plan

## P0 — 可运行闭环
**P0-01 工程骨架**：FastAPI/配置/DI/日志/OTel/health；建立 domain/application/ports/adapters 分层。DoD：服务启动、lint/typecheck/test 可执行。

**P0-02 PostgreSQL + migrations**：实现 family/version/snapshot/execution/node/attempt/artifact/runtime_event/self_test/outbox 等基础表与 repository。DoD：空库 migration up/down 或等价回滚策略；唯一约束覆盖版本与 Attempt。

**P0-03 领域状态机**：Workflow/Execution/Node 显式迁移与历史。DoD：合法/非法/终态迁移单测。

**P0-04 Agent Preparation + Snapshot**：Port、候选解析、start/health/readiness/reachability、snapshot freeze/hash。DoD：不合格 Agent 无法 Binding。

**P0-05 Execution IR + Validator**：DAG、binding、schema、artifact/recovery metadata；环检测/引用校验。DoD：IR-DAG/BIND/SCHEMA/SEC 规则测试。

**P0-06 Compiler + n8n Adapter**：IR→n8n JSON；import/validate/publish/execute/get_execution。DoD：领域层无 n8n 依赖；fake + adapter contract test。

**P0-07 S0~S4 Self-Test**：门禁、证据、failure signature、有限修复。DoD：未通过门禁不可 publish/execute。

**P0-08 Execution/Node/Attempt**：正式执行、idempotency、技术 retry vs business attempt。DoD：重复请求不重复副作用。

**P0-09 Artifact/MinIO**：登记 ref/hash/schema/producer；不在 DB 存正文。DoD：写失败可幂等重试；hash 校验。

**P0-10 Runtime Event + Reconciliation**：事件 ingest/normalize；缺失时 n8n+Agent+MinIO 对账。DoD：故障注入可形成 reconciliation result。

**P0-11 CMM + Outbox**：get_view、append_event port；本地事务写 outbox，异步幂等发送；CMM 降级。DoD：CMM 断开不阻断已发布执行。

**P0-12 E2E**：Plan→Snapshot→IR→SelfTest→Publish→Execution→Artifact→terminal→CMM。DoD：核心验收全部通过。

## P1 — 增量恢复
P1-01 Checkpoint/input_fingerprint；P1-02 Evaluation/quality/reusable；P1-03 invalidation/downstream closure/Resume Root；P1-04 PATCH 新版本谱系与 reused_nodes/invalidated_nodes。DoD：V1 前序成功、后序低质量时 V2 不重跑可信前序节点。

## P2 — 自优化
P2-01 最近 N 次历史压缩/Memory Summary；P2-02 failure_signature 有效修复检索；P2-03 成功结构/Agent 版本经验辅助生成。DoD：优化只影响建议/生成策略，不破坏确定性校验和发布门禁。

## 每任务通用 DoD
代码、单测、必要集成测试、migration/contract 更新、traceability 更新、IMPLEMENTATION_STATUS 更新；所有测试通过。
