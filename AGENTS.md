# Codex 开发执行规则

## 1. 目标
依据 `source_docs/执行编排智能体总体设计方案_V3.2.docx` 与 `source_docs/执行编排智能体_详细设计文档_V1.0.docx` 实现“执行编排智能体”。目标是生产级、可运行、可测试、可演进的工程，不是 Demo。后续开发基于'/home/test/执行编排智能体_Codex工程开发包_V1.0/codex_dev_pack' 这个项目中的代码，已完成功能的无需再次生成

## 2. 设计权威顺序
1. 总体设计 V3.2：业务/架构基线。
2. 详细设计 V1.0：工程化展开。
3. 本开发包 `specs/`、`contracts/`：为编码便利做的结构化提取。
4. 若发生冲突：停止相关实现，在 `ASSUMPTIONS.md` 记录冲突，不自行改变业务语义。

## 3. 不可违反的架构约束
- Execution IR 是平台无关稳定中间层；n8n 只是当前编译/执行目标。
- Workflow Version 不可变。结构、Agent Binding 或 Resume Root 改变必须创建新版本。
- Registry Snapshot 在 Workflow 生成前冻结；生成、自测、正式执行使用同一快照约束。
- 未通过完整发布门禁的 Workflow 不得正式执行。
- CMM 是上下文/记忆视图，不是运行状态唯一事实源。
- PostgreSQL/Runtime Event/n8n/Agent/MinIO 按职责保存运行事实；事件缺失时支持三方对账。
- 大对象正文放 MinIO，编排侧只保存 Artifact Ref、Hash、Schema、Producer、质量/权限/复用元数据。
- 所有关键副作用必须有稳定 Idempotency Key。
- 技术重试保持同一 Attempt；业务重新执行创建新 Attempt。
- Secret 不得出现在模型生成内容、IR、日志或数据库业务字段中，只使用 Credential Ref。
- CMM/历史记忆不可用允许降级，但绝不绕过发布前自测。

## 4. 实施纪律
- 先阅读本文件、README、ASSUMPTIONS、IMPLEMENTATION_PLAN、ARCHITECTURE、DOMAIN_MODEL、STATE_MACHINES、DATABASE、API_SPEC、ACCEPTANCE_TESTS。
- 先建立需求追踪矩阵，再编码。
- P0 完成并测试通过前，不实现 P1/P2 的复杂优化。
- 每个模块：实现 + 单元测试 + 必要集成测试 + 文档同步。
- 外部系统必须通过 Port/Adapter；领域层禁止直接 import n8n/MinIO/CMM/Agent Runtime SDK。
- 所有状态迁移必须由显式 transition/service 控制，不允许任意字段赋值改变状态。
- 所有 DB schema 变化必须提供 migration。
- 对 `TBD-*` 不得擅自“永久定案”。如为使 P0 可运行必须选择默认值，必须：配置化、写入 ASSUMPTIONS、测试覆盖、可替换。
- 不得为了让测试通过而删除约束、弱化门禁或 mock 掉核心领域规则。

## 5. 每阶段完成条件
每阶段结束必须执行 lint/typecheck/unit/integration（以实际技术栈命令为准），并更新 `IMPLEMENTATION_STATUS.md`：已完成、测试结果、遗留 TBD、下一步。失败先修复再继续。

## 6. 第一条 Codex 指令
不要一次性生成全部代码。先审阅开发包，输出拟采用的工程目录、依赖、关键接口与 `TBD-*` 处理策略；确认设计之间无冲突后，再从 P0-01 开始逐任务实现。
