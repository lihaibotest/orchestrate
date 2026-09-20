# State Machines

> 状态名来自详细设计的工程建议；若跨系统正式枚举确认后，通过 mapping 调整。

## Workflow Version
DRAFT → VALIDATING → SELF_TESTING → VERIFIED → PUBLISHED
失败路径：VALIDATING/SELF_TESTING → SELF_TEST_FAILED。
PUBLISHED 为不可变终点；修复产生 child version。

## Execution
CREATED → RUNNING → SUCCEEDED
CREATED → FAILED
RUNNING → FAILED
RUNNING → PATCHING → RUNNING/FAILED
RUNNING → REPLANNING → RUNNING/FAILED
SUCCEEDED/FAILED 为终态。

## Node
PENDING → READY → RUNNING → SUCCEEDED → EVALUATED
RUNNING → RETRY_WAIT → RUNNING（同 Attempt 技术重试）
RUNNING/RETRY_WAIT → FAILED
SUCCEEDED/EVALUATED → INVALIDATED（上游/Hash/修复边界失效）
INVALIDATED 在新业务 Attempt/新版本中重新进入执行链，不允许静默覆盖历史 Attempt。

## 状态机实现要求
- 使用显式 transition API，校验 from/to。
- transition 与状态历史记录同事务。
- 非法迁移抛领域错误并产生审计日志。
- 终态不可回退；恢复通过新 Attempt/新 Workflow Version 表达。
