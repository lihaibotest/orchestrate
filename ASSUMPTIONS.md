# Assumptions / TODO / 待确认事项

原则：以下事项源设计未最终固化。Codex 不得把临时值写死为不可配置的业务事实。

| ID | 待确认项 | 临时开发规则 |
|---|---|---|
| TBD-01 | 任务编排输入正式 JSON Schema / 完成标准表达 | 先定义内部 DTO + JSON Schema，Adapter 层可转换；不得把内部 DTO 宣称为上游最终协议 |
| TBD-02 | Agent Registry/Runtime API、health/readiness 语义 | 先定义 Port；提供 fake adapter 用于测试，真实 adapter 留配置与 TODO |
| TBD-03 | CMM get_view/append_event 正式接口 | 定义 CmmPort；payload 采用可扩展对象，保持 event_id 幂等 |
| TBD-04 | n8n 版本、节点模板、导入/发布 API | 定义 WorkflowEnginePort/N8nAdapter；n8n 细节集中在 adapter |
| TBD-05 | Evaluation decision 枚举、ACCEPTED/低质量阈值 | 领域层用可配置 decision mapping/threshold；恢复逻辑不硬编码第三方枚举 |
| TBD-06 | 最大修复次数、技术重试、退避、超时 | 全部配置化；不得散落 magic numbers |
| TBD-07 | Artifact Hash、Schema Registry、input_fingerprint | 定义策略接口；默认实现可采用 SHA-256，但必须可替换并标注为临时工程选择 |
| TBD-08 | 认证、租户鉴权、Credential 管理 | API 留 Auth/TenantContext 边界；Credential 只传 ref；不得实现明文 secret 存储 |
| TBD-09 | TPS/并发/P95/保留周期 | 先实现可观测指标和配置点，不声称达到未定义 SLA |
| TBD-10 | Workflow/Execution/Runtime Event 正式跨系统枚举 | 内部枚举与 adapter mapping 分离 |

## 额外说明
详细设计中的 `context.token_budget.next_workflow_generation=24000` 是设计建议；应配置化。详细设计明确自测修复阈值需要配置化；如源总体设计存在示例默认值，也不要让默认值成为领域不变量。
