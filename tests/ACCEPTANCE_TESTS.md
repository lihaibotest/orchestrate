# Acceptance & Fault Injection Tests

## P0 必测
1. Agent 未启动：可按策略启动；仍不健康则不得进入 Binding。
2. Agent health 正常但 n8n 不可达：preflight/Self-Test 阻断发布。
3. Snapshot 创建后 Registry 发生变化：既有版本仍使用冻结 Snapshot；需要变化时创建新版本。
4. IR 有环/edge 指向不存在节点：S0 前确定性阻断。
5. Binding 与 Snapshot 不一致：阻断发布。
6. 明文 Credential/Secret 出现在 IR：阻断发布并脱敏日志。
7. n8n import 失败：记录 failure signature；按有限修复策略处理。
8. S3 Canary Agent/MinIO 失败：不得正式发布。
9. 重复创建 Execution 请求：同 idempotency key 不产生重复副作用。
10. 同 Attempt 技术 retry：attempt_no 不变；业务重新执行：attempt_no 增加。
11. MinIO 写失败：幂等重试；最终失败节点进入失败路径。
12. Runtime Event 丢失：n8n+Agent+MinIO 对账形成可审计结论。
13. CMM 不可用：已发布执行继续；新生成按降级规则且仍完整自测。
14. 终态后 Workflow/Execution/Artifact/SelfTest/Outbox 可关联追溯。

## P1 必测
15. Artifact hash 被篡改：禁止复用，传播 invalidation。
16. Schema 不兼容/输入指纹变化/权限失败：禁止复用。
17. A→B→C→D，A/B accepted，C low-quality：新版本从 C 恢复，A/B 不重跑。
18. 分叉 DAG 中单支失败：只 invalidated 受影响后继，独立支可复用。
19. 多 seed 失败：Resume Root 计算覆盖全部 invalidated nodes。
20. Checkpoint 不可信/缺失：不得从错误边界恢复。

## 通过证据
API 响应、DB 状态/状态历史、n8n execution ref、runtime events、artifact refs/hash、checkpoint、self-test evidence、trace/correlation、outbox/CMM event 均应可追踪。
