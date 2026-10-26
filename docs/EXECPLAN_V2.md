# ExecPlan V2.0 接口适配

本工程现支持 `planning-blueprint.v4` / `orchestration.v2.0`，同时保留
`planning-blueprint.v3` 的历史兼容入口。

V2.0 请求是 path-only ExecPlan。每个 Task 只接收一个严格匹配 Dashboard
`enname` 的 `capability_ref`，以及同一注册快照解析出的 `input_paths`、
`output_paths`、`log_path` 和 `artifact_path`。请求体不接受 Schema、Artifact
Storage、WHOLE_ARTIFACT/FIELD_MAPPING wire 字段，也不接受运行时 ID、endpoint、
credential、worker 或 queue。

`registration_snapshot.digest` 必须是 `sha256:` 加 64 位小写十六进制摘要；
`idempotency_key` 固定为 `{plan_id}:v{plan_version}`。`INITIAL` 不得携带
Replan 血缘，`REPLAN` 必须携带前一版本和 `replan_context`。`traceparent`/
`tracestate` 只作为 HTTP/MQ 传输头，不进入 V2.0 请求体。

Execution Submit 现在与任务编排统一通过 `POST /api/v2/blueprints` 接收。
请求可以直接发送完整 ExecPlan，也可以使用
`{"exec_plan": <完整 V2.0 对象>}` 包装；该入口拒绝
`launch_context.evaluation_id`，并将 `Idempotency-Key` 与请求体中的固定幂等键
交给同一 V2.0 入站校验链。历史 `POST /api/execution/v1/runs` 和
`POST /api/v1/planning-blueprints` 继续保留，以支持已有调用方迁移。

## 从已持久化计划恢复

如果任务编排智能体已将完整 ExecPlan 持久化到执行编排库，但执行编排服务在
生成/发布前重启，使用 `scripts/start_latest_execplan.py`。该脚本只读最新的
`planning_blueprint_ingestion.payload_json`，并将同一不可变载荷重放到
`POST /api/execution/v1/runs`。已有的 `plan_id + plan_version` 幂等键会阻止
新建第二个计划，同时允许执行编排服务继续生成、Self-Test 和发布。

脚本默认只恢复到发布阶段；创建正式运行必须显式传入 `--start` 和运行时输入
文件，避免从计划路径猜测测试目标。示例：

```bash
export ORCHESTRATOR_POSTGRES_DSN='postgresql://...'
python scripts/start_latest_execplan.py --dry-run
python scripts/start_latest_execplan.py
python scripts/start_latest_execplan.py --start --inputs-file /path/to/runtime-inputs.json
```

在当前部署中，PostgreSQL 主机名 `pg-fresh` 只在 Docker 网络内可解析。宿主机
不应把示例中的 `...` 当作 DSN 主机名；优先使用以下包装脚本，使查询在
`p009-execution-service` 容器内执行并复用其 DSN 和 submit token：

```bash
scripts/start_latest_execplan_in_container.sh --dry-run
scripts/start_latest_execplan_in_container.sh --plan-id <plan_id> --plan-version <version>
scripts/start_latest_execplan_in_container.sh --start --inputs-file /absolute/path/runtime-inputs.json
```

包装脚本不会打印或复制数据库 DSN、Bearer token；只会把本仓库的 Python 启动脚本
和显式指定的运行时输入 JSON 临时复制到容器的 `/dev/shm`（内存临时目录）。

`runtime-inputs.json` 必须是 JSON object，例如 `test_path`、`timeout`、
`generate_allure` 和 `use_xvfb`。生产环境应使用实际数据库 DSN；不要将 DSN
或提交 token 写入脚本、ExecPlan、日志或版本库。

若该计划的 generation 已是 `PUBLISHED`，脚本不会将持久化 JSON 再次走入站
适配器，而是直接使用数据库中已冻结的 `workflow_family_id` 与
`workflow_version`。这是必要的：持久化载荷是规范化记录，而入站的幂等哈希保持
上游 Task 列表的原始顺序；把前者重投会被正确地拒绝为内容不一致。

宿主机 `28020` 是纯 HTTP/1.1 端口，不是 TLS 端口；调用地址必须使用
`http://127.0.0.1:28020`。将 `https://`、TLS ClientHello 或 HTTP/2 prior-knowledge
直接发送到该端口会被 Uvicorn 在路由前拒绝，并记录 `Invalid HTTP request received.`。
请求还必须携带 `planning-agent` 的 `X-Orchestrator-*` HMAC 请求头；缺少或错误的
签名会返回 401，而不是 400。

当 `ORCHESTRATOR_POSTGRES_DSN` 已配置时，容器必须保留
`PYTHONPATH=/app/backend:/app/vendor/python`。后一个目录包含离线打包的
`psycopg2-binary`；将 `PYTHONPATH` 覆盖为只有 `/app/backend` 会在持久化
Blueprint 时触发 `ModuleNotFoundError: psycopg2` 并导致 500。

如果任务编排运行在 Docker 容器中，28020 不能只绑定 `127.0.0.1`；应绑定
`0.0.0.0`，或让调用方使用同一 Docker 网络中的
`http://p009-execution-service:8020`。

适配器将 path-only 字段保存为不可变的 V4 规范化载荷，并在进入既有 IR 边界时
使用内部兼容表示；旧 V3 的 schema/artifact wire 语义不会被添加回 V4 请求，V4
不会生成旧版 Artifact Declaration。
V4 数据库约束由 migration `0011_planning_blueprint_v4` 放宽 schema version，
down 会删除 V4 记录后恢复 V3-only 约束。

V2.0 的注册快照内容解析和跨 Task 路径搬运仍以规范第 18 节开放项为准；本次
仅实现规范示意的 submit wrapper，不扩展路径协议或运行时 Envelope。
