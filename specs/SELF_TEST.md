# Workflow Self-Test / Release Gate

任何新 Workflow Version 正式发布前必须完成完整门禁。

| Level | 内容 | 典型证据 |
|---|---|---|
| S0 | JSON/DAG、node id、连接完整性 | validator report, ir/workflow hash |
| S1 | Agent Binding、Endpoint、Credential Ref、I/O Schema | snapshot diff, schema report |
| S2 | n8n import、trigger/webhook | workflow id, import result |
| S3 | Canary Agent + MinIO I/O | canary execution, test artifact |
| S4 | E2E monitoring/evaluation/checkpoint/terminal callback | events, evaluation, checkpoint, terminal status |

失败必须记录 `failure_stage`, `error_signature`, `repair_action`。自动修复次数有限且配置化；结构/Binding/Resume Root 的修复必须创建新 Workflow Version。能力缺失、凭据策略拒绝、不可兼容 Schema 等不可修复类别应终止/升级，而不是无限生成。
