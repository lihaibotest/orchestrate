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

## 设计审查记录（2026-09-20）

以下问题来自对 `AGENTS.md`、源设计基线、`specs/`、`contracts/`、`db/schema.sql`、验收测试和追踪矩阵的交叉审查。它们是编码前的实现假设/阻塞项记录，不改变源设计的业务语义；在相关 TBD 或外部契约确认前，代码必须通过 Port、配置和可替换 Adapter 隔离。

### REVIEW-01：结构化契约约束不足

- `contracts/execution-ir.schema.yaml` 将 `agent_binding` 声明为 `additionalProperties: true`，没有明确禁止 Secret，也没有强制 `agent_id/version/endpoint/schema/credential_ref` 的结构；`edges` 未关闭额外字段，`artifacts`、`checkpoints` 只有宽泛 object。
- `contracts/runtime-event.schema.yaml` 没有把详细设计要求的 `agent_id`、`attempt`、`duration_ms`、`artifact_refs`、`checkpoint_ref` 等作为统一最小必填集合，`payload` 也未定义敏感字段边界。
- `contracts/context-event.schema.yaml` 缺少跨租户/Workflow Family 的关联约束，不能单独保证事件可追溯和幂等。
 - P0-11 CMM 外部协议仍为 TBD-03；本实现只定义 `CMMPort.get_view`/`append_event`，不假设服务端字段或 endpoint。`cmm_context_event` 与 `cmm_outbox` 通过 0010 migration 独立持久化，旧 P0 migration 只在显式 P0-11 opt-in 时应用。
- 处理策略：P0-05 采用内部不可变 DTO + 确定性 Validator；Schema 文件只作为外部契约，补充版本化约束、Secret 扫描、身份一致性和闭合对象规则，直到上游协议确认前不宣称为最终协议。

### REVIEW-02：API 契约不完整

`contracts/openapi.yaml` 目前只有创建 Family、建版本、自测、发布、创建/查询 Execution 和 Runtime Event 的骨架，缺少详细设计中的 Agent Preparation/Snapshot、Artifact 注册、Repair、Reconcile、Checkpoint、状态查询的请求/响应 Schema、安全方案和租户约束。P0-01 只建立可演进的 API 版本与 DTO 边界；P0-12 前必须补齐并由契约测试锁定，不能用未定义字段直接实现外部兼容性。

### REVIEW-03：数据库基线缺少关键事实与约束

当前 `db/schema.sql` 缺少 Evaluation、Repair/Version lineage、Reconciliation、Agent Binding 明细和可查询的 Self-Test gate 汇总；`workflow_version.parent_version` 没有复合外键；`workflow_version.registry_snapshot_id` 不能保证 Snapshot 属于同一 Family；Execution 的 `client_request_id` 可为空导致唯一索引无法覆盖“同一业务请求”；Execution 没有 `started_at/ended_at`；Node 没有 `current_attempt/evaluation/checkpoint`；Snapshot/Published Version 没有不可变数据库约束；状态字段多为无约束 text；Outbox 没有 aggregate/idempotency hash、lease/in-flight、last error 和锁领取字段；Runtime Event 没有校验其版本与 Execution 一致。

处理策略：P0-02 使用 Alembic 迁移重建完整基线，增加复合外键、CHECK/部分唯一索引、不可变触发器、Evaluation/Repair/Reconciliation/Idempotency/Outbox lease 等模型。任何 destructive migration 必须显式审批；不能通过放宽约束迁就实现。

### REVIEW-04：状态机存在未定义边界

- Execution 的 `PATCHING/REPLANNING → RUNNING` 没有明确强制创建 child Workflow Version；状态机未定义取消、超时、对账中和外部操作待确认状态。
- Node 的 `EVALUATED` 没有对应 Evaluation 实体/契约；技术 retry 与业务新 Attempt 的状态边界虽有文字约束，但 SQL/状态历史尚未表达。
- Workflow `PUBLISHED` 的退休/归档语义未在状态机和数据库中定义。

处理策略：P0-03 实现显式 transition service、状态历史和版本谱系检查；跨系统状态通过 TBD-10 mapping，未知外部状态进入显式 `UNKNOWN/RECONCILIATION_REQUIRED` 映射而非静默转换。未确认的业务枚举不写成不可配置常量。

### REVIEW-05：跨文档身份字段不一致

总体设计和数据库以 `workflow_family_id` 为主，详细设计的调用标准又要求 `session_id`，现有 Runtime Schema 只要求 `execution_id/workflow_version`。目前没有定义 `session_id` 与 `workflow_family_id` 的一对一/生命周期关系。

处理策略：P0-01/P0-05 在内部统一使用 `tenant_id + workflow_family_id + workflow_version + execution_id + correlation_id`；外部 `session_id` 只通过版本化 Adapter mapping 接入。mapping 未确认前不得把两个字段当作同义词或丢弃任一字段。

### REVIEW-06：外部依赖缺少可执行契约

Agent Registry/Runtime、CMM、n8n、MinIO、监控 Agent 的真实 API、健康/就绪语义、凭据协议和错误枚举均未随包提供；三方对账需要的 n8n/Agent/MinIO 查询接口也未定义。P0 只能先实现 Port、Fake Adapter、契约测试和配置化真实 Adapter；不将 Fake 行为误认为生产集成已验证。

### REVIEW-07：CMM/运行事实边界需要落到模型

源设计同时要求终态/运行/Artifact/Checkpoint 事实回写 CMM，又规定 CMM 不是运行状态唯一事实源。实现必须把 PostgreSQL、Runtime Event、n8n、Agent、MinIO 作为各自事实源，CMM 只接收不可变 ContextEvent/视图；Outbox payload 只能携带引用、Hash、Provenance 和受控摘要，不能把 CMM 当运行态写入库。

### REVIEW-08：自测与评估协议未闭合

S4 要求监控、Evaluation、Checkpoint 和终态回传，但 Evaluation 表、协议、`ACCEPTED/REWORK` 等 decision 枚举和阈值尚未定义。P0-07 只能通过 `EvaluationPort`、可配置 decision mapping/threshold 和 Fake Adapter 完成门禁骨架；真实枚举确认前不得硬编码质量业务语义。

### REVIEW-09：恢复与 Artifact 约束需要补充

`db/schema.sql` 的 Artifact/Checkpoint 只能保存 JSON 引用，缺少 producer 结构、schema compatibility 证据、权限校验结果和 invalidation lineage；当前 IR Schema 也没有强制 recovery 元数据完整性。P0-05/P0-09/P1-01~04 必须以 canonical hash、schema registry Port、input fingerprint Port 和显式 invalidation 记录实现，禁止把“存在 URI”当作可复用。

### REVIEW-10：容量、安全和保留策略不是已确认事实

详细设计给出 PostgreSQL、Redis、Kubernetes、OTel/Prometheus/Grafana 的推荐技术，但 TPS、P95、并发、保留周期、认证/租户协议和哈希算法仍为 TBD。初始化方案采用可配置默认值和观测能力，不对未确认 SLA、认证协议或数据留存作生产承诺；生产上线前必须完成容量/安全评审。

## P0-01 实施假设

- ASSUMPTION-P001-01：便携包继续使用已确认的 Python 3.10 运行基线，不在本阶段升级至设计建议的 Python 3.11+。

## P0-02 实施假设

- ASSUMPTION-P002-01：现有 SQLite v1/v2 持久化暂时保留用于兼容；新的结构化控制面事实由 PostgreSQL 保存，SQLite 不成为目标事实源。
- ASSUMPTION-P002-02：相关用例迁移完成前，进程启动不强制 PostgreSQL 可用；部署和 migration 通过 secret-backed 配置接收凭据，示例环境文件不得包含密码。
- ASSUMPTION-P002-03：P0-02 只接入 Redis 配置，不引入 Redis Adapter；Redis 不是最终事实源。
- ASSUMPTION-P002-04：离线数据库访问使用 vendored CPython 3.10 `psycopg2-binary` 2.9.13；在线安装使用 `pyproject.toml` 声明范围，驱动保持在 Adapter 内可替换。
- ASSUMPTION-P002-05：便携包采用带 SHA-256 校验、事务 up 和显式 latest-version down 的 SQL migration runner；若后续统一 Alembic，可替换而不改变领域语义。
- TBD-09：容量、保留、分区和性能目标仍未定义；当前索引和约束只保证正确性，不声明未验证 SLA。
- TBD-10：状态字段使用结构化规格当前词汇；跨系统状态映射仍属于 Adapter，未由 migration 永久定案。

## P0-03 实施假设

- ASSUMPTION-P003-01（P0-07 修订）：Workflow Version 结构字段保持不可变，生命周期 `status` 只能通过显式矩阵改变；`PUBLISHED` 为终态。`SELF_TEST_FAILED -> SELF_TESTING` 仅允许未改变内容的显式重测；改变 IR/结构/Binding/Resume Root/编译语义的修复必须创建子版本。
- TBD-P003-01：设计未定义 Execution 从 `PATCHING`/`REPLANNING` 返回 `RUNNING` 时与子 Workflow Version 的持久化关联；P0-03 不擅自修改 `execution.workflow_version`。
- TBD-P003-02：保留规定的 Node `EVALUATED` 状态；Evaluation 实体和 decision 词汇推迟到对应任务，不从运行状态推断质量结论。

## P0-04 实施假设

- TBD-02 / TBD-P004-01：Agent Registry API、候选排序、注册错误词汇及 Runtime start/health/readiness/reachability API 未定义。P0-04 使用 Port、确定性 agent/version 顺序和 Fake Adapter；真实协议 Adapter 可替换。
- ASSUMPTION-P004-01：同一候选可满足多个 capability requirement；单次准备中每个 `(agent_id, version)` 只执行一次生命周期检查。失败候选记录 rejection 后尝试下一确定性候选，全部需求成功前不持久化 Snapshot。
- ASSUMPTION-P004-02：`credential_ref` 是不透明非空引用；P0-04 不解析或保存 Credential 值。Credential Manager 协议、引用格式、授权与轮换仍待确认。
- TBD-P004-02：Schema Registry/JSON Schema dialect 未确定前，只执行确定性结构校验和 canonical JSON 校验；完整 meta-schema 校验保持可替换。
- ASSUMPTION-P004-03：Snapshot 在 Workflow Family 内按内容寻址。SHA-256 输入为 canonical JSON，包含格式版本、Family、算法及按 requirement 排序的 bindings，不包含随机 `snapshot_id` 和 `created_at`；相同内容返回既有不可变 Snapshot。
- ASSUMPTION-P004-04：P0-02 通用 `RegistrySnapshot` 允许 legacy/external hash label，migration 0003 不追加会破坏兼容性的 digest CHECK。P0-04 强类型模型要求小写 64 位 SHA-256；共享表收紧需要所有 producer 迁移后的独立兼容 migration。

## P0-05 实施假设

- ASSUMPTION-P005-01：正式平台无关契约使用 `execution-ir.v1` / `ir_version=1.0`，字段以源详细设计 4.1/4.2 为基线。迁入工程中原有的 `ExecutionIRV2` 属于旧便携链路兼容模型，不作为 P0-05 Domain 真相；其迁移/弃用需在调用方切换时单独处理。
- TBD-01 / ASSUMPTION-P005-02：上游 Task Plan 和 Completion Criteria 正式协议未定义。P0-05 接收内部强类型模型并把 `completion_criteria` 保持为冻结 JSON 对象，不把当前字段名宣称为上游最终协议。
- TBD-07 / ASSUMPTION-P005-03：Schema Registry 和 JSON Schema dialect 未确定。当前规则校验基本结构；Agent Binding I/O Schema 必须与 Snapshot 完全一致；Edge 兼容采用保守的 type、required property、array item 和 enum 子集判断。算法集中在 Validator，可由正式 Schema Port 替换。
- ASSUMPTION-P005-04：Execution IR 内容 Hash 使用 canonical JSON + SHA-256，排序不影响 Hash。该选择只用于 IR 身份，不永久确定 Artifact Hash 算法。
- ASSUMPTION-P005-05：只有 `invoke_agent` 节点强制 Agent Binding；其他节点可在后续协议明确时绑定评估/存储能力。任何存在的 Binding 均必须来自同一冻结 Snapshot。
- ASSUMPTION-P005-06：P0-05 只验证 Recovery metadata 的引用、互斥、Resume Root 覆盖和 reused predecessor 关系；完整失效传播、Checkpoint 信任计算和 Resume Root 算法按计划留在 P1，不在本阶段伪装为已实现。
- ASSUMPTION-P005-07：P0-05 不新增数据库表。验证通过后的 `ir_hash` 可写入 P0-02 已有不可变 Workflow Version；结构、Binding 或 Resume Root 改变仍必须创建新版本。

## P0-06 实施假设

- TBD-04 / ASSUMPTION-P006-01：源设计未固定 n8n 版本、正式导入/校验/发布/执行 API 与响应 Schema。P0-06 通过 `WorkflowEnginePort` 隔离，并把仓库现有 versioned gateway 作为可替换 Adapter 契约；Fake/recording gateway 契约测试通过不代表真实 n8n 生产联调已完成。
- TBD-P006-01：P0-05 IR 只枚举 `decision`、`wait`、`evaluate`、`load_artifact`、`store_artifact`、`checkpoint`，但未定义表达式、时长、评估 Binding 或 Artifact/Checkpoint 调用参数。P0-06 对这些节点返回稳定的 `N8N-NODE-001` 编译错误，不生成空操作或硬编码业务规则；正式平台无关字段确认后再扩展 IR 和 Adapter。
- ASSUMPTION-P006-02：确定性映射仅覆盖 `invoke_agent -> HTTP Request`、`join -> Merge`、`end -> No Operation`。技术重试映射为 n8n 节点 retry，保持同一业务 Attempt；业务 Attempt 仍由 P0-08 控制。
- TBD-08 / ASSUMPTION-P006-03：`credential_ref` 保持不透明引用，并通过可注入 resolver 映射为 n8n Credential ID；默认 identity 仅用于现有 gateway/Fake 契约，不读取或生成 Secret。
- REVIEW-05 / ASSUMPTION-P006-04：编译后的调用表达式同时保留运行输入中的 `session_id` 和 IR 的 `workflow_family_id`，不会把二者互相替代。`correlation_id`、`execution_id`、`attempt` 和幂等键在运行时注入；正式跨系统 envelope 仍待接口评审。
- TBD-10 / ASSUMPTION-P006-05：`WorkflowExecution.status` 保留外部字符串并仅在 Adapter 中解析；正式 n8n 到领域状态映射留给执行控制/对账阶段，P0-06 不把 n8n 状态直接写成领域事实。
- ASSUMPTION-P006-06：P0-06 不新增数据库结构。S0~S4 门禁属于 P0-07；虽然 Port/Adapter 提供 publish/execute 能力，本阶段不新增可绕过门禁的 API 路由。

## P0-07 实施假设

- ASSUMPTION-P007-01：`SELF_TEST_FAILED` 不再是绝对终态，但只允许由 Self-Test repository 执行 `SELF_TEST_FAILED -> SELF_TESTING`，且受持久化 `repair_attempt` 和配置上限控制；不得直接 SQL 绕过。
- TBD-06 / ASSUMPTION-P007-02：最大修复次数通过 `ORCHESTRATOR_SELF_TEST_MAX_REPAIR_ATTEMPTS` 配置，默认值 3 只是部署默认值，不是 Domain 不变量；Domain `SelfTestPolicy` 接受显式注入值。
- ASSUMPTION-P007-03：未改变 IR 和编译语义的暂态故障可对同一未发布版本重测；任何语义修复创建带 `parent_version`、repair reason 和 failure signature 的新 Workflow Version，并重新从 S0 开始。
- TBD-04 / ASSUMPTION-P007-04：S2 使用 P0-06 WorkflowEnginePort/Fake 和 gateway 契约；未完成真实 n8n 联调，不把 Fake PASS 宣称为真实服务器 PASS。
- ASSUMPTION-P007-05：P0-09 前 S3 只通过 `ArtifactProbePort` 保存 write/hash/reference 元数据，不实现 Artifact 正文或 MinIO 管理；真实 MinIO 状态保持未验证。
- ASSUMPTION-P007-06：P0-10 前 S4 只通过 `ObservabilityProbePort` 验证最小完成/监控/终态证据，不建立 Runtime Event reconciliation，也不把 CMM 当运行事实源。
- ASSUMPTION-P007-07：P0-07 不实现 AI 修复算法；`WorkflowRepairPort` 的 Fake 只验证闭环语义。相同失败签名由 canonical stable fields 的 SHA-256 生成，不包含时间、UUID、trace ID 或动态 message。

## P0-08 实施假设

- ASSUMPTION-P008-01：正式 Execution 的业务幂等身份采用 `tenant_id + workflow_family_id + workflow_version + client_request_id`。API `Idempotency-Key` 必须存在并作为已作用域的传输幂等输入校验，但显式 `client_request_id` 不因 HTTP 重试键变化而产生第二个 Execution；同一业务身份下请求内容变化返回冲突。
- ASSUMPTION-P008-02：`X-Tenant-Id` 仅是 TBD-08 AuthContext/TenantContext 接缝。P0-08 通过 Workflow Family 所有权查询和仓库查询范围保护租户边界，不声称已实现认证协议。
- ASSUMPTION-P008-03：P0-08 使用 P0-06 `WorkflowEnginePort` 的 operation key 语义；Fake 已验证外部副作用后本地响应丢失时重放不重复执行，真实 n8n 仍属于 TBD-04 live conformance 风险。
- ASSUMPTION-P008-04：Attempt `attempt_no`、技术 `retry_count`、Self-Test `repair_attempt` 是三个独立计数器。重试上限/退避/超时从冻结 IR retry policy 读取，默认值不成为 Domain 不变量，具体协议仍属 TBD-06。
- ASSUMPTION-P008-05：P0-08 只提供 RuntimeObservation/Checkpoint reference Port seam，不实现 P0-10 Runtime Event ingestion/reconciliation、P1-01 trusted Checkpoint/input fingerprint、P1 recovery lineage 或 MinIO Artifact 正文管理。

## P0-09 实施假设

- TBD-07 / ASSUMPTION-P009-01：Artifact content hash 当前采用集中在 Adapter 边界的 SHA-256；正式算法协商未定，不把 MinIO ETag 当内容 Hash。Schema Registry 方言未定，P0-09 只校验 schema_ref 一致性与确定性的 JSON Schema 子集（type/required/properties/items/enum/additionalProperties）。
- ASSUMPTION-P009-02：Artifact identity 使用 `execution_id + node_id + attempt_no + output_name`。同 Attempt 技术重试不创建身份；相同身份不同内容作为冲突拒绝。显式 Business Attempt 2 创建并保留新 Artifact identity。
- ASSUMPTION-P009-03：MinIO storage ref 对 Domain 是不透明引用；实际 key 仍按 tenant、execution mode、Workflow、Execution、Node、Attempt、output 分区。SELF_TEST 与 PRODUCTION 通过独立路径段隔离。
- TBD-08 / ASSUMPTION-P009-04：`X-Tenant-Id` 仍是 Auth/TenantContext 接缝，不代表完整认证；Repository 和 Artifact Manager 强制 tenant + artifact_id 查询，并以 404 隐藏跨租户对象。
- ASSUMPTION-P009-05：P0-02 已有 `input_fingerprint`/quality/reusable 字段只为向后兼容保留；P0-09 不计算或信任这些字段，不实现 Checkpoint、Resume Root、recovery lineage 或 Artifact intelligence。
- ASSUMPTION-P009-06：Agent Runtime 正式输出回调协议仍未固化；`complete_agent_execution` 是 P0-09 Application integration seam。P0-10 才把 runtime event ingest/normalization/reconciliation 接到该接缝。
- INTEGRATION-RISK-P009-01：Fake MinIO client 已覆盖 PUT/HEAD/GET/delete、响应丢失和篡改；真实 MinIO TLS、IAM、bucket policy、retention 和 SDK/服务版本 conformance 需要部署环境验证，不能由 Fake PASS 替代。

## planning-blueprint.v3 入站适配假设

- ASSUMPTION-BPV3-01：`planning-blueprint.v3` 是外部 inbound contract；内部真相是不可变 `PlanningBlueprintPlan` 和后续 Execution IR，不直接复用外部 Pydantic model。
- ASSUMPTION-BPV3-02：上游 `workflow_version` 仅作为请求元数据保留；内部 Workflow Version 由生成组合根分配，当前 Fake generation contract 使用显式注入的版本。
- ASSUMPTION-BPV3-03：`X-Tenant-Id` 是 TBD-08 AuthContext/TenantContext 接缝，不代表已完成认证。所有 inbound 幂等和持久化查询仍按 tenant 作用域。
- ASSUMPTION-BPV3-04（INT-01B 修订）：生产组合使用 `ProductionPlanningBlueprintGenerationService` 复用现有 Snapshot、IR、Compiler、Self-Test、Publish 和 Formal Execute；Fake 只作为外部 Registry/Runtime/Engine/Probe 的 CI contract double，不代表真实 n8n/MinIO 联调。
- ASSUMPTION-BPV3-05：只支持 `WHOLE_ARTIFACT`；`FIELD_MAPPING` 明确拒绝。Artifact Manager、Runtime Event/Reconciliation 分别保留给 P0-09/P0-10。
- ASSUMPTION-P010-01：外部 n8n、Agent Runtime 和 Artifact Store 只提供不可变 RuntimeEvent/RuntimeObservation；Execution/Node Truth 只由 ReconciliationService 经 StateTransitionService 计算。
- ASSUMPTION-P010-02：事件处理失败保留事件并增加 processing_retry_count；事件处理重试永远不创建新的 Business Attempt。
- ASSUMPTION-P010-03：source/external_reference/payload_hash 仅作为辅助去重键，event_id 是主幂等身份；乱序事实按状态终态和规则处理，不采用简单 last-event-wins。
- ASSUMPTION-P010-04：P0-10 只提供基础 poll/recovery seam，不实现 P1 Resume Root、trusted Checkpoint、input fingerprint、recovery lineage、Evaluation、FIELD_MAPPING 或 Artifact Intelligence。
- ASSUMPTION-BPV3-06：实际上游文件可不在 body 提供 `correlation_id`；Adapter 优先使用可信请求上下文 `X-Correlation-Id`，未提供时以 `session_id` 作为可追踪 fallback，但不把两者视为同一生命周期身份。
- INTEGRATION-ISSUE-BPV3-02：`task-agent-current-execplan.json` 的 `word-generate.depends_on=case-execute` 不对应任何 task_id，且输入引用 `collect-api-info:api-spec` 不存在。系统按设计返回 `PLAN_DAG_INVALID`，不会猜测或静默改写上游业务语义。
- INTEGRATION-ISSUE-BPV3-01：工程 source_docs 未找到精确文件名“执行编排智能体总体设计方案_V3.2_格式规范版”；使用同版本 V3.2 docx 与工作区提供文档内容完成适配核对。
