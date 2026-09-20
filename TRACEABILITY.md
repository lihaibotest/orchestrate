# Traceability Matrix

| Requirement | 实现模块 | 测试 |
|---|---|---|
| Execution IR 引擎无关 | domain/ir + compiler port | IR validator + compiler contract |
| Workflow Version 不可变 | workflow aggregate/repository | immutable/version lineage tests |
| Registry Snapshot 冻结 | agent preparation | snapshot hash/mutation tests |
| S0~S4 发布门禁 | selftest | release gate E2E |
| 技术 retry ≠ 新 Attempt | execution aggregate | retry/attempt tests |
| Artifact 大对象仅 MinIO | artifact port/adapter | persistence boundary test |
| Artifact 复用重验 | recovery service | hash/schema/fingerprint/permission tests |
| CMM 非运行事实源 | cmm adapter/outbox | CMM outage test |
| Runtime Event 丢失三方对账 | reconciliation | event-loss fault injection |
| Credential Ref only | IR validator/security | secret rejection test |
| Resume Root 增量恢复 | recovery domain | DAG recovery tests |
