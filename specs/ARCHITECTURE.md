# Architecture

## 主链路
Plan Intake → Agent Preparation → Registry Snapshot → CMM Context Assembly → Execution IR Builder/Validator → Workflow Compiler → S0~S4 Self-Test → Immutable Workflow Version Publish → Execution Control → Artifact/Checkpoint/Runtime Event → CMM Sync。

## 分层
- API/Application：用例编排、事务边界、DTO。
- Domain：Workflow Family/Version、Execution、Node、Attempt、Artifact、Checkpoint、状态机、恢复算法、幂等规则。
- Ports：AgentRuntimePort、WorkflowEnginePort、ObjectStorePort、CmmPort、Event/MonitoringPort、Repository ports。
- Adapters：n8n、MinIO、CMM、Agent Registry/Runtime、PostgreSQL、Redis。
- Infrastructure：配置、OTel、metrics、logging、migration、deployment。

## 推荐 Python 包结构
```text
src/execution_orchestrator/
  api/
  application/
    commands/ queries/ services/
  domain/
    workflow/ execution/ artifact/ recovery/ selftest/ events/
  ports/
  adapters/
    postgres/ n8n/ minio/ cmm/ agent_runtime/ redis/
  infrastructure/
  config/
tests/unit tests/integration tests/e2e
migrations/
```

## 依赖规则
Domain → 无基础设施依赖；Application → Domain + Ports；Adapters → Ports + 外部 SDK；API → Application。禁止 Domain/Application 直接调用 n8n/MinIO/CMM SDK。

## 事实源边界
- PostgreSQL：控制面结构化元数据、状态、幂等、Outbox、审计索引。
- n8n：Workflow Engine 执行事实之一。
- Agent Runtime：Agent 生命周期/健康事实。
- MinIO：Artifact/Checkpoint 大对象事实。
- Runtime Event：运行事件事实。
- CMM：Context/Memory 视图，不替代运行控制事实。
