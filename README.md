# 执行编排智能体 — Codex 工程开发包

本目录用于直接交给 Codex/编码智能体实施工程。它不是源设计的替代品，而是把 V3.2 总体设计与 V1.0 详细设计转成更适合逐项编码和验收的规格。

## 当前实施状态

本目录现已同时作为规格与实现仓库。P0-01 至 P0-04 的已验收实现已从
`portable-migration` 合并至此，P0-05 Execution IR/Validator 与 P0-06
Compiler/n8n Adapter、P0-07 S0~S4 Self-Test 与发布/执行门禁也在此实现；
P0-08 已补齐正式 Execution、NodeExecution、Business Attempt、同 Attempt
技术重试、稳定幂等身份与 PostgreSQL 事务控制；P0-09 已实现 Artifact/MinIO
Manager、输出验证、ArtifactRef 与 WHOLE_ARTIFACT 下游交接；
后续开发以本目录为唯一工程根目录，不再向旧目录生成新功能。

为避免把发布制品混入源码仓库，合并范围包含源码、配置模板、migration、
测试、脚本和 vendored Python 数据库驱动，不包含离线容器镜像、运行数据、
日志、状态目录或真实 Secret。

## 建议技术基线
源详细设计推荐：Python 3.11+ / FastAPI、PostgreSQL、Redis、n8n、MinIO、CMM、Agent Registry/Runtime、Kubernetes、OpenTelemetry + Prometheus + Grafana。除既有平台组件外，其余属于推荐实现，可按现网技术栈等价替换，但不得破坏 Execution IR 与 Adapter 边界。

## 目录
- `AGENTS.md`：Codex 必须遵守的开发规则（首先阅读）
- `ASSUMPTIONS.md`：全部待确认项及临时实现规则
- `IMPLEMENTATION_PLAN.md`：P0/P1/P2 任务顺序与 DoD
- `IMPLEMENTATION_STATUS.md`：实施过程状态台账
- `specs/ARCHITECTURE.md`：组件与依赖边界
- `specs/DOMAIN_MODEL.md`：核心领域对象/不变量
- `specs/STATE_MACHINES.md`：Workflow/Execution/Node 状态机
- `specs/SELF_TEST.md`：S0~S4 发布门禁
- `specs/ARTIFACT_RECOVERY.md`：Artifact/Checkpoint/Resume Root
- `specs/OBSERVABILITY_SECURITY.md`：可观测性与安全约束
- `contracts/execution-ir.schema.yaml`：Execution IR 初始契约
- `contracts/openapi.yaml`：编排 API 初始契约骨架
- `contracts/runtime-event.schema.yaml`：运行事件契约
- `contracts/context-event.schema.yaml`：CMM 事件契约
- `db/schema.sql`：建议 PostgreSQL 初始逻辑模型
- `tests/ACCEPTANCE_TESTS.md`：验收与故障注入清单
- `TRACEABILITY.md`：设计要求→实现/测试映射
- `source_docs/`：原始总体设计与详细设计

## 开始方式
把整个目录作为仓库根目录或复制到目标仓库。给 Codex 的首条命令建议为：

“阅读 AGENTS.md 和整个开发包。先输出实现计划和 TBD 处理策略，不要一次性生成全部代码；然后严格按 IMPLEMENTATION_PLAN.md 从 P0 开始，每完成一项运行测试并更新 IMPLEMENTATION_STATUS.md。”
