# Evaluator–Orchestrator Separation and Workflow Self-Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Separate evaluator traffic from the Orchestrator while making the Orchestrator autonomously repair observable workflow/runtime faults and escalate only capability or plan-boundary changes to the Planning Agent.

**Architecture:** n8n sends execution telemetry (attempts, node errors, outputs, logs, usage, and checkpoints) to the Orchestrator. Evaluator nodes send node-level and final quality payloads directly to the evaluator, which reports its opinions directly to the Planning Agent. The Orchestrator never consumes or relays evaluator opinions; it uses only deterministic runtime facts, the current WorkflowSpec/Execution IR, Registry snapshot, budgets, and checkpoints to classify and repair workflow-local faults. New workflow versions are created only after material change, validation, and publication gates pass.

**Monitoring-absent fallback:** At compilation time the Orchestrator determines whether the immutable Registry snapshot has a usable monitoring/evaluator binding and whether the compiled workflow contains at least one monitoring node. If either condition is false, the Orchestrator records `planner_final_summary_fallback` for that execution lineage. When that lineage reaches a terminal state, it sends the Planning Agent exactly one `workflow-final-summary.v1` through a dedicated outbox. The summary contains only Orchestrator-owned execution facts: node and workflow status, bounded logs, output/artifact references, input summary, checkpoints, timing, retry/repair history, and correlation/version identity. It must never contain an evaluator opinion, quality score, verdict, or raw evaluator payload. This is an observability fallback, not an evaluator relay and not a repair command.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, SQLite, n8n HTTP/Webhook nodes, pytest, existing v1/v2 contracts and offline test doubles.

---

## Frozen boundaries

1. Evaluator and Orchestrator have no evaluation-result channel between them.
2. Evaluator sends quality/capability opinions directly to Planning Agent.
3. Orchestrator receives only runtime telemetry from n8n and performs deterministic self-repair.
4. Orchestrator must not forward evaluator payloads to Planning Agent. When monitoring is absent, it may send its own terminal execution summary under the `workflow-final-summary.v1` contract; that summary cannot contain evaluator data.
5. Orchestrator may emit a separate escalation fact only when its own checks prove that the current workflow cannot proceed without a new capability, external dependency, or plan-level change.
6. Existing workflow versions are immutable. Self-repair publishes a new inactive version and never overwrites the active/history record.
7. “Optimal” means technically valid, executable, bounded, schema-compatible, and accepted by static/Canary gates. Business-quality optimality remains the evaluator/Planning Agent responsibility because evaluator opinions are intentionally not visible to Orchestrator.
8. Monitoring absence is determined from the immutable Registry snapshot and compiled workflow structure, never from a failed request to an evaluator endpoint. A direct evaluator call failure remains an ordinary runtime failure and follows the retry/repair/escalation policy.

## Approved plan amendment: monitoring-absent final-summary fallback

This amendment is explicitly authorized by the user after the preceding review freeze. It changes no evaluator/Orchestrator boundary: it adds only a Planning-Agent delivery of Orchestrator-owned terminal execution facts when no monitoring agent is registered or the generated workflow has no monitoring node. It initially refines the implementation and acceptance scope of F-03, F-05, F-06, F-08, and F-13. The subsequent frozen-plan review identified F-15 as a regression caused by this amendment; after it is recorded below, the plan is frozen again.

## Task 1: Freeze the channel and ownership contracts

**Files:**
- Create: `backend/contracts/evaluator_channel.py`
- Modify: `backend/contracts/runtime_event.py`
- Modify: `backend/contracts/orchestration.py`
- Create: `schemas/v2/workflow-repair-decision.v1.json`
- Test: `tests/contracts/test_evaluator_orchestrator_separation.py`

**Step 1: Write failing tests**

Add tests asserting:

- runtime telemetry accepts node/attempt/checkpoint data without evaluator opinion fields;
- a repair decision has `workflow_local`, `external_boundary`, and `uncertain` classifications;
- an evaluator-to-Planning-Agent payload contains evaluation data but no Orchestrator repair command;
- evaluator opinion payloads cannot be accepted as an Orchestrator runtime event.
- `WorkflowFinalSummaryV1` accepts only terminal runtime facts and bounded artifact/log references, and recursively rejects evaluator/quality/opinion fields.

**Step 2: Run the focused tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/contracts/test_evaluator_orchestrator_separation.py
```

Expected: FAIL because the channel and repair-decision contracts do not exist.

**Step 3: Implement the minimal contracts**

Define strict, bounded models for:

- `RuntimeTelemetryV1` (execution/node facts only);
- `WorkflowRepairDecisionV1` (`retry_same_version`, `repair_new_version`, `escalate`, `stop`);
- `WorkflowFinalSummaryV1` with `correlation_id`, `session_id`, `plan_version`, `workflow_version`, `execution_id`, terminal status, `monitoring_mode`, node/workflow evidence references, bounded output summary, checkpoint references, retry/repair history, and an idempotency key;
- Document the evaluator-to-planner payload as an external boundary; do not add an evaluator callback model to the Orchestrator runtime API.

`monitoring_mode` is one of `evaluator_direct` or `planner_final_summary_fallback`. `planner_final_summary_fallback` may only be selected if the immutable Registry snapshot lacks a usable evaluator binding or the compiler result has zero monitoring nodes. Reject evaluator identity, evaluator payload, quality score, quality verdict, and repair directive fields recursively.

Do not add endpoint, credential, secret, or n8n JSON fields to any model.

**Step 4: Re-run focused tests**

Expected: all tests in the new contract file pass.

## Task 2: Compile evaluator nodes as direct evaluator calls

**Files:**
- Modify: `backend/backends/n8n/compiler_v2.py`
- Modify: `backend/services/n8n_workflow_compiler.py`
- Modify: `backend/services/planning_job_runner.py`
- Modify: `backend/services/external_adapters.py`
- Modify: `tests/backends/test_n8n_v2_compiler.py`
- Modify: `tests/characterization/test_v1_api_and_artifacts.py`
- Modify: `tests/e2e/doubles/evaluator.py`

**Step 1: Write failing tests**

Assert that when an evaluator is registered:

- node and final evaluator HTTP nodes target the evaluator endpoint from the immutable Registry snapshot;
- they do not target `/api/v2/runtime/evaluations` or any Orchestrator evaluation relay route;
- evaluator payloads contain execution/artifact/log references and identity fields;
- business Agent runtime events still target the Orchestrator runtime event endpoint.

Add a no-evaluator test confirming that evaluator nodes are omitted while runtime telemetry remains present.

Add fallback characterization tests confirming that a missing evaluator binding, or an evaluator-disabled/zero-monitoring-node compiled artifact, produces `planner_final_summary_fallback`; a registered evaluator and at least one compiled monitoring node produces `evaluator_direct`. A failed evaluator HTTP request must not switch an already compiled lineage to fallback.

**Step 2: Run focused compiler tests**

Expected: FAIL because v2 currently compiles evaluator nodes to the Orchestrator gateway evaluation paths.

**Step 3: Implement direct evaluator binding**

Pass a safe evaluator binding (`endpoint`, credential reference, evaluator ID, snapshot hash) from `PlanningJobRunner` to the v2 compiler. Keep endpoint/credential selection outside the LLM. Reuse the existing v1 direct-evaluator behavior and align payload fields across v1/v2. Persist the compiler-selected monitoring mode and monitoring-node count with the immutable workflow/version record so a restart cannot change the delivery policy.

**Step 4: Re-run compiler and characterization tests**

Expected: all direct-routing assertions pass and the legacy golden hash is updated only if the intentional routing change modifies it.

## Task 3: Remove the evaluator relay from Orchestrator

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/services/external_adapters.py`
- Modify: `backend/services/decision_relay_service.py`
- Modify: `backend/security/hmac_auth.py`
- Modify: `tests/api/test_blueprint_v2_api.py`
- Modify: `tests/api/test_runtime_events.py`
- Modify: `tests/characterization/test_v1_api_and_artifacts.py`

**Step 1: Write failing API tests**

Assert that:

- runtime event submission stores telemetry and never calls a planner evaluation endpoint;
- no Orchestrator route accepts evaluator opinions as a normal callback in the new v2 channel;
- the old v1 evaluation relay is either removed or returns an explicit migration response (`EVALUATOR_DIRECT_CHANNEL_REQUIRED`) without forwarding data;
- duplicate runtime events remain idempotent.
- a terminal workflow with `planner_final_summary_fallback` creates one summary outbox operation, while a terminal workflow with `evaluator_direct` creates none;
- final-summary delivery contains only Orchestrator-owned facts and never calls the legacy evaluation relay.

**Step 2: Run focused API tests**

Expected: FAIL because `main.py` currently calls `control.relay_evaluation()` for evaluation callbacks and failure diagnosis.

**Step 3: Remove relay behavior**

Delete the evaluator-to-planner relay from Orchestrator control flow. Keep Planning Agent authentication for blueprint/directive endpoints. If the legacy v1 route must remain for migration, make it an explicit non-forwarding rejection and document the replacement direct evaluator channel. The new terminal-summary outbox operation is distinct from the rejected legacy evaluator route and serializes only `WorkflowFinalSummaryV1`.

**Step 4: Re-run API and security tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/api tests/security/test_v2_contract_security.py
```

Expected: all tests pass with no evaluator payload leaving Orchestrator.

## Task 4: Add deterministic workflow-local failure classification

**Files:**
- Create: `backend/services/workflow_failure_classifier.py`
- Modify: `backend/services/failure_classifier.py`
- Modify: `backend/contracts/runtime_event.py`
- Test: `tests/services/test_workflow_failure_classifier.py`

**Step 1: Write failing tests**

Cover these categories:

- transient technical failure -> same-version retry;
- input/output binding mismatch -> workflow repair;
- invalid node dependency or checkpoint mismatch -> workflow repair;
- missing Registry capability or unavailable external dependency -> escalation fact;
- ambiguous/unclassified failure -> bounded repair attempt, then escalation;
- business-quality-only result is not consumed by Orchestrator as an evaluator opinion.

**Step 2: Run the tests**

Expected: FAIL because the classifier currently only distinguishes technical versus nontechnical failure.

**Step 3: Implement deterministic classification**

Use only Orchestrator-owned facts. Never inspect evaluator quality fields. Return a bounded decision with reason code, node ID, current version, repair attempt, and required evidence.

**Step 4: Re-run focused tests**

Expected: all classifier tests pass.

## Task 5: Create an Orchestrator-owned repair coordinator

**Files:**
- Create: `backend/services/workflow_repair_coordinator.py`
- Modify: `backend/services/workflow_repair_service.py`
- Modify: `backend/services/ir_repair_service.py`
- Modify: `backend/persistence/execution_plan_repository.py`
- Modify: `backend/history/orchestration_repository.py`
- Test: `tests/services/test_workflow_repair_coordinator.py`
- Test: `tests/persistence/test_v2_repository_atomicity.py`

**Step 1: Write failing tests**

Assert that the coordinator:

- retries transient failures without changing `workflow_version`;
- sends workflow-local diagnosis to the repair generator without evaluator data;
- rejects unchanged repair output;
- increments version only after a material candidate is validated;
- caps self-repair attempts and prevents repair loops;
- preserves the old active version when a candidate fails validation or Canary;
- emits an escalation fact only for a confirmed external boundary.
- reloads the persisted monitoring mode and terminal-summary idempotency key after restart without changing an `evaluator_direct` lineage to fallback.

**Step 2: Run focused tests**

Expected: FAIL because repair is currently triggered only by external `patch/replan` directives and the v2 IR repair helper is not wired into runtime handling.

**Step 3: Implement the coordinator**

The coordinator must:

1. load current WorkflowSpec/IR and immutable Registry snapshot;
2. assemble only runtime facts, bounded logs, input summary, and checkpoint evidence;
3. call the appropriate repair generator;
4. validate identity, schema, graph, bindings, side effects, budgets, and backend support;
5. require a material IR/WorkflowSpec change;
6. publish through the durable validate/create-inactive/Canary/activate/start sequence;
7. record repair attempt, reason code, old/new hashes, publication state, monitoring mode, monitoring-node count, and terminal-summary delivery state in the durable local state machine.

Do not pass evaluator results into this coordinator.

**Step 4: Re-run focused persistence and service tests**

Expected: all repair and atomicity tests pass.

## Task 6: Integrate self-repair into v1/v2 runtime handling

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/api/v2.py`
- Modify: `backend/runtime/supervisor.py`
- Modify: `backend/services/retry_runtime.py`
- Modify: `backend/services/outbox_dispatcher.py`
- Test: `tests/runtime/test_supervisor.py`
- Test: `tests/api/test_runtime_events.py`
- Test: `tests/e2e/test_v2_full_lifecycle.py`

**Step 1: Write failing integration tests**

Cover:

- transient node failure resumes the same version;
- workflow-local schema/binding failure automatically creates a new version without a Planning Agent directive;
- an external-boundary failure creates an escalation record but does not generate a speculative workflow;
- evaluator payloads are absent from the repair request;
- old workflow remains active until the repaired workflow passes publication gates;
- duplicate callbacks cannot trigger duplicate repairs.
- when monitoring is absent, terminal completion posts one final execution summary to Planning Agent; repeated callbacks, outbox retries, and restart recovery cannot post a second logical summary;
- when a monitoring node exists, terminal completion does not post the fallback summary even if the evaluator later becomes unreachable.

**Step 2: Run focused integration tests**

Expected: FAIL because current runtime handling only retries or relays diagnosis, and `patch/replan` is externally initiated.

**Step 3: Wire the coordinator**

Route runtime event failures through:

```text
RuntimeSupervisor
  -> WorkflowFailureClassifier
  -> same-version retry OR WorkflowRepairCoordinator OR escalation record
  -> terminal `workflow-final-summary.v1` outbox operation only when the persisted monitoring mode is `planner_final_summary_fallback`
```

Keep Planning Agent directives as an explicit external override, not the normal path for workflow-local repair.

**Step 4: Re-run integration tests**

Expected: all runtime and lifecycle tests pass.

## Task 7: Define the Planning Agent escalation and monitoring-absent final-summary contracts

**Files:**
- Modify: `backend/contracts/orchestration.py`
- Create: `schemas/v2/workflow-escalation.v1.json`
- Create: `schemas/v2/workflow-final-summary.v1.json`
- Modify: `docs/external-agent-contracts-v2.md`
- Modify: `README.md`
- Test: `tests/contracts/test_escalation_contract.py`

**Step 1: Write failing contract tests**

Require escalation messages to contain:

```text
correlation_id
session_id
plan_version
workflow_version
reason_code
required_decision
missing_capabilities or external_dependency
repair_attempts
evidence references
```

Reject evaluator quality scores and raw evaluator opinions in this Orchestrator-generated escalation contract.

Require final summary messages to contain:

```text
correlation_id
session_id
plan_version
workflow_version
execution_id
terminal_status
monitoring_mode=planner_final_summary_fallback
node execution states
output/artifact/log/checkpoint references
input summary
retry/repair history
idempotency key
```

They must exclude evaluator identity, evaluator payload, quality verdict, quality score, recommendation, and repair directive. The payload has reference and bounded-summary limits; it does not upload arbitrary artifact bytes.

**Step 2: Implement and document the contract**

Clarify that evaluator sends its own quality opinion directly to Planning Agent, while Orchestrator sends only independently verified execution-boundary facts. Define separate authenticated outbox operations: `send_workflow_escalation()` for confirmed plan boundaries and `send_workflow_final_summary()` only for the monitoring-absent fallback. Each operation has its own endpoint/path, caller identity, operation key, retry policy, persistence state, and restart recovery; neither may reuse `relay_evaluation()`.

**Step 3: Re-run contract tests**

Expected: all escalation schema tests pass.

## Task 8: Update test doubles and end-to-end coverage

**Files:**
- Modify: `tests/e2e/doubles/evaluator.py`
- Modify: `tests/e2e/doubles/planning_agent.py`
- Modify: `tests/e2e/doubles/n8n_gateway.py`
- Modify: `tests/e2e/test_v2_pipeline_with_doubles.py`
- Modify: `tests/e2e/test_v2_full_lifecycle.py`
- Create: `tests/e2e/test_evaluator_channel_separation.py`

**Step 1: Write failing E2E tests**

Verify:

- evaluator receives node and final evaluation payloads directly from generated workflow;
- Planning Agent receives evaluator opinions directly from the evaluator double;
- Orchestrator receives runtime events only;
- workflow-local failures are repaired without a Planning Agent directive;
- capability-boundary failures create only an escalation fact;
- evaluator opinions never appear in Orchestrator repair requests or runtime decisions.
- absent evaluator binding and zero compiled monitoring nodes cause the n8n double to complete without evaluator calls, then cause exactly one Orchestrator final-summary delivery to the Planning Agent double;
- a direct evaluator workflow sends evaluator opinion directly to Planning Agent and produces no fallback summary;
- duplicate final callbacks and restart recovery preserve final-summary idempotency.

**Step 2: Run E2E tests**

Expected: FAIL until routing and coordinator integration are complete.

**Step 3: Update doubles and assertions**

Make the test evaluator explicitly call the Planning Agent double. Make the n8n double expose separate telemetry and evaluator channels.

**Step 4: Re-run the full E2E subset**

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/e2e
```

Expected: all separation and lifecycle tests pass.

## Task 9: Documentation and migration review

**Files:**
- Modify: `README.md`
- Modify: `docs/external-agent-contracts-v2.md`
- Create: `docs/evaluator-orchestrator-separation.md`
- Modify: `manifest.json` if contract/schema inventory changes
- Modify: `tests/test_development_bundle_contents.py`

Document:

- the two independent data paths;
- Orchestrator self-repair scope;
- evaluator direct-to-Planning-Agent scope;
- escalation conditions;
- monitoring-absent detection, the `workflow-final-summary.v1` payload, delivery ownership, idempotency, and the distinction between this fallback and evaluator relay;
- the legacy evaluation relay removal/deprecation behavior;
- the fact that business-quality optimality is not evaluated by Orchestrator;
- required environment/configuration for direct evaluator delivery;
- version immutability and bounded repair attempts.

Run documentation/schema inventory tests and reject any evaluator source or secret accidentally added to the package.

## Task 10: Full regression and release gate

Run in order:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/contracts tests/blueprint tests/capabilities tests/planning
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/runtime tests/services tests/api tests/backends tests/verification
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/e2e
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider
```

Acceptance checklist:

- no evaluator payload is accepted by or forwarded through the Orchestrator evaluation path;
- evaluator nodes target evaluator directly;
- Planning Agent receives evaluator opinions directly;
- when the immutable snapshot has no evaluator binding or the compiled workflow has no monitoring node, Planning Agent receives one Orchestrator-owned terminal final summary; otherwise it receives no such fallback summary;
- final-summary payloads contain facts and references only, never evaluator opinions or quality verdicts;
- Orchestrator repairs workflow-local failures without Planning Agent intervention;
- external-boundary escalation contains facts, not evaluator opinions;
- same-version retries preserve idempotency;
- repaired versions are materially changed and immutable;
- old versions remain recoverable;
- publication is durable and idempotent;
- all existing non-evaluator functionality remains green;
- development bundle manifests and source map remain consistent.

Because this is a non-Git workspace, do not add git commit/reset/checkout steps. Each task must still follow red-test, minimal implementation, green-test, and review evidence discipline.

---

##封版审查记录（2026-08-19）

本 plan 自本节起冻结。审查期间不继续增加产品需求、架构方向或新的 finding。后续只允许：

1. 关闭下列既有 finding；或
2. 记录由修订直接引入的 regression；或
3. 记录本次因外部条件无法验证的项目，并保留为 open。

### 审查范围和当前证据

已检查：

- 本 plan 与真实 v1/v2 API、runtime、compiler、repair、publisher 代码；
- `manifest.json`、`docker-compose.yml`、`backend/Dockerfile`、`.env.example`；
- `artifacts/SHA256SUMS`、image manifest、SPDX SBOM、image tar；
- SQLite migration/repository、outbox、publication state 和 runtime decision 表；
- 全部现有 pytest、制品内容测试、hash/tamper/layer scan 脚本。

新鲜证据：

```text
pytest -q -p no:cacheprovider => 145 passed, 1 skipped
pytest artifact/development bundle subset => 6 passed
pytest persistence/runtime budget subset => 4 passed
sha256sum -c artifacts/SHA256SUMS => all 成功
scripts/verify-package.sh => passed
```

外部条件：

```text
docker compose config
```

未能在未提供部署环境变量时完成，因为 `N8N_API_KEY_FILE` 等 Compose secret 文件变量是
必需输入。该命令未被修改或绕过；已完成静态 Compose/Docker 检查和现有制品验证。该项
属于外部条件，不能作为代码通过证据。

开发版签名状态已核对：`manifest.json` 明确 `artifact_signing=false`，README 明确这是
unsigned development bundle；因此本 plan 不新增签名需求。运行时 HMAC secret 仍由
Compose mounted secret 文件提供。

### 唯一 P0–P3 findings 清单

以下是本次审查后的唯一 finding 集合。没有 P0；所有 finding 初始状态为 `open`，在实施
完成并回归复审前不得标记为 `closed`。

#### F-01 — P1 — runtime 通道仍可接收 evaluator 结果

- **文件：** `backend/contracts/runtime_event.py`、`backend/api/v2.py`、`backend/backends/n8n/compiler_v2.py`、Task 1/2。
- **风险：** `RuntimeEventV2` 仍允许 `evaluation_completed`，且 `metrics`/`failure` 是开放对象；v2 compiler 仍指向 `/api/v2/runtime/evaluations` 和 `/final`。evaluator 意见可以进入 Orchestrator，违反冻结的完全分离边界。
- **修复：** runtime telemetry 只允许执行事实；删除或明确拒绝 `evaluation_completed` 和 evaluator/quality 字段；v2 evaluator 节点必须直连 evaluator；旧 evaluation 路径不得写库或进入 repair 输入。
- **验收：** 发送 evaluator payload 到 runtime/evaluation 路径得到明确 4xx/410；数据库、outbox 和 repair request 无新增记录；编译产物只将 runtime event 指向 Orchestrator，将 evaluator payload 指向 evaluator。
- **回归证据：** `tests/contracts/test_evaluator_orchestrator_separation.py` 验证 runtime 拒绝 `evaluation_completed`、quality/evaluator 嵌套字段；`tests/backends/test_n8n_v2_compiler.py` 验证 evaluator 节点直连 immutable binding，业务 runtime event 仍指向 Orchestrator；全量 `pytest -q -p no:cacheprovider` 为 `150 passed, 1 skipped`。
- **状态：** closed。

#### F-02 — P1 — legacy `/api/v1/evaluations` 行为和认证未冻结

- **文件：** `backend/main.py:254-266`、`backend/history/orchestration_repository.py:271`、`backend/security/hmac_auth.py`、`docker-compose.yml`、Task 3。
- **风险：** 当前路由会认证、落库并调用 `relay_evaluation()`；plan 同时允许“删除路由”或“返回迁移响应”。而 Compose 没有挂载 `ORCHESTRATOR_EVALUATOR_HMAC_SECRET_FILE`，迁移响应可能在业务逻辑前直接 500。
- **修复：** 冻结为保留路由但返回 `410 EVALUATOR_DIRECT_CHANNEL_REQUIRED`，不解析、不落库、不 relay；或彻底移除路由。若保留 410，必须在认证之前完成迁移拒绝，并删除 evaluator caller 权限/secret 依赖。
- **验收：** 精确状态码和响应体断言；数据库 evaluations/callbacks 无变化；无 planner HTTP 调用；无 evaluator secret 时仍返回确定的迁移响应。
- **回归证据：** `tests/api/test_runtime_events.py::test_legacy_evaluation_endpoint_is_a_non_forwarding_migration_response` 验证没有 evaluator secret 时 `/api/v1/evaluations` 仍返回 `410 EVALUATOR_DIRECT_CHANNEL_REQUIRED`，且 evaluations/callbacks 不新增记录；`main.py` 已删除 v1 event failure 分支的 `relay_evaluation()` 调用；全量 `pytest -q -p no:cacheprovider` 为 `150 passed, 1 skipped`。
- **状态：** closed。

#### F-03 — P1 — evaluator→Planning Agent 的生产协议未定义

- **文件：** plan Architecture/Task 2/8/9、`backend/services/external_adapters.py`、`tests/e2e/doubles/evaluator.py`。
- **风险：** Registry 只提供 evaluator endpoint/credential，没有 evaluator 如何找到 Planning Agent、认证、请求 schema、幂等键、重试、失败持久化和响应语义。仅由 test double 直接调用无法证明可迁移部署。
- **修复：** 在外部 agent contract 中冻结 evaluator-owned direct channel 的 endpoint、认证、payload、Planner 地址配置、幂等/重试和失败处理；该协议不能变成 Orchestrator relay。同时冻结 Orchestrator-owned `workflow-final-summary.v1` fallback 的独立 endpoint、认证、幂等/重试和失败恢复；它仅在不存在监控 binding 或编译产物没有监控节点时发送。
- **验收：** E2E evaluator double 通过真实 direct adapter 投递给 planner double；Planner 能验证 identity/idempotency；Orchestrator 没有观察或转发该请求。缺少监控时，Planner 仅收到一次事实型 final summary，且不含 evaluator 字段。
- **回归证据：** `tests/contracts/test_evaluator_channel.py` 覆盖固定 endpoint/path、caller identity、HMAC、完整执行身份、幂等、超时、重试和 evaluator-owned 失败持久化，并由 evaluator double 通过 direct adapter 投递到 Planning Agent double；`tests/api/test_runtime_events.py` 验证 Orchestrator 仅创建自己的 fallback summary。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-04 — P1 — IR repair API 仍显式接收 evaluator opinion

- **文件：** `backend/services/ir_repair_service.py:59-74`、Task 5。
- **风险：** `IRRepairService.repair()` 接收 `evaluation` 并把它序列化到 `ir-repair-request.v1`。仅新增 coordinator 而不修改该 API，会继续把 evaluator 意见泄露给 Orchestrator repair LLM。
- **修复：** 重命名为 `runtime_facts`/`runtime_assessment`，递归禁止 `evaluation`、quality score、evaluator opinion 等字段；repair 请求只允许 runtime-owned facts。
- **验收：** 负向测试递归扫描 repair payload，任何 evaluator/quality 字段均被拒绝；成功 repair payload 不包含 evaluator 数据。
- **回归证据：** `tests/services/test_ir_repair_service.py` 验证 repair API 只接收 `runtime_facts`，允许合法动作名 `REQUEST_EVALUATION`，并递归拒绝 `quality_score`、`evaluation_id`、`evaluator_opinion`；`tests/e2e/test_v2_full_lifecycle.py` 验证实际 repair payload 仅含 runtime facts。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-05 — P1 — escalation 没有独立投递通道

- **文件：** Task 6/7、`backend/services/external_adapters.py`、`backend/services/decision_relay_service.py`。
- **风险：** plan 既说 Orchestrator 可以向 Planning Agent 发送 escalation fact，又只要求“创建 escalation record”；没有 endpoint、认证、幂等、重试、outbox 或明确的 persist-only 语义，实施结果会分叉。
- **修复：** 冻结独立 `send_workflow_escalation()` 和 `send_workflow_final_summary()` 通道，均与 evaluator channel 分开；定义各自 endpoint、HMAC、operation/idempotency key、outbox 状态和失败恢复。不得复用 `relay_evaluation()`。
- **验收：** Planning Agent 只收到 escalation facts 或在监控缺失时收到一次 final summary；两类消息均不含 evaluator opinion；重复投递只产生一个逻辑操作；外部失败可从 outbox 恢复。
- **回归证据：** `tests/contracts/test_orchestration_delivery.py` 和 `tests/persistence/test_planning_agent_outbox.py` 验证两个独立 endpoint/caller/schema/operation key/outbox kind、失败重试、重启恢复和重复抑制，且不存在 evaluation relay kind；全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-06 — P1 — v2 自修复所需的 WorkflowSpec/Registry 无法重启恢复

- **文件：** `backend/services/planning_job_runner.py`、`backend/persistence/execution_plan_repository.py`、`backend/history/orchestration_repository.py`、Task 5。
- **风险：** v2 当前只持久化 IR/hash 和安全的 Agent ID；没有完整 WorkflowSpec、编译 artifact、endpoint/credential binding 或不可变 Registry candidate 快照。进程重启后 coordinator 无法按 plan 要求重新编译修复版本。
- **修复：** 持久化完整 spec/IR、artifact、snapshot hash 与可安全恢复的 binding metadata；敏感 credential 只保存 reference；并持久化监控模式、监控节点数量和 final-summary operation key；将恢复与 publication state 放入 durable state machine。
- **验收：** 在每个 publication 阶段崩溃并重启，能恢复当前版本/快照/修复输入和既定监控投递策略，不重复副作用、repair 或 final summary，不丢历史 workflow。
- **回归证据：** migration v7 与 `tests/persistence/test_execution_plan_recovery_context.py` 验证完整 WorkflowSpec/IR、compiled artifact、immutable Registry snapshot、安全 binding reference、monitoring mode/count 和 final-summary key 可重启恢复，并拒绝明文 secret；migration v10 及 terminal 测试补齐终态恢复。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-07 — P1 — 外部 n8n 发布状态不能声称 SQLite 原子化

- **文件：** Task 5/10、`backend/services/durable_publisher.py`、`backend/services/workflow_publisher.py`。
- **风险：** SQLite 事务不能和 n8n create/activate/start 构成单一原子事务；“记录 publication status atomically”表述不明确，可能导致错误的双写实现。
- **修复：** 明确 durable saga 状态：`candidate_created → validated → inactive_created → canary_passed → activated → started`；每步使用 operation key、outbox、lookup/reconciliation 和 crash recovery。
- **验收：** 每个 crash point 只会产生一次外部副作用；旧 active 版本在新版本完成前保持可用；状态机可重放且最终一致。
- **回归证据：** `tests/services/test_durable_publisher.py` 覆盖 `prepared`、`validated`、`inactive_created`、`canary_passed`、`activated`、`started`、`reconciled` 七阶段 operation key、外部已提交超时 reconciliation、逐 crash point 重启和重复副作用抑制，并验证 Canary 失败时旧 active workflow 保持可用。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-08 — P2 — RuntimeTelemetry 版本命名与现有契约冲突

- **文件：** Task 1/6、`backend/contracts/runtime_event.py`、现有 v1 `N8NEvent`。
- **风险：** plan 新增 `RuntimeTelemetryV1`，但现有路径同时使用 `RuntimeEventV2` 和 v1 `N8NEvent`，未定义 V1 是否新公开 API、内部模型还是适配层。
- **修复：** 优先继续使用 `RuntimeEventV2` 作为公开 runtime telemetry 契约；若必须引入 V1，定义 V1→V2 adapter、endpoint 和兼容测试。
- **验收：** v1/v2 请求映射明确；evaluation_completed 和 evaluator fields 在两个入口均被拒绝；终态运行事实可按 `workflow-final-summary.v1` 投递，但不能改变 runtime telemetry 为 evaluator channel。
- **回归证据：** `tests/contracts/test_runtime_telemetry_mapping.py` 验证公开 `RuntimeEventV2` 与 legacy `N8NEvent` 映射到内部 `RuntimeTelemetryV1`，保持执行身份并在两个入口递归拒绝 evaluator-owned 字段；terminal facts 使用独立 `RuntimeTerminalEventV2`。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-09 — P2 — self-repair 上限和计数范围未定义

- **文件：** Task 4/5/6/9、数据库 repair 状态设计。
- **风险：** 计划只写“bounded/caps”，没有数值、按 node/workflow/session/version 的作用域，也没有 uncertain 路径的持久化计数，可能无限生成修复版本。
- **修复：** 冻结默认上限、配置字段、计数键和耗尽动作；建议按 session + workflow lineage 持久化，达到上限后只产生一次 escalation/stop。
- **验收：** 第 N 次达到上限后不再生成候选版本；重启不重置计数；重复事件不重复 escalation。
- **回归证据：** migration v8 与 `tests/services/test_workflow_repair_coordinator.py::test_repair_cap_is_persisted_per_session_and_workflow_lineage` 验证默认上限 2、session + workflow lineage 计数、重启不重置、耗尽后只产生一次 escalation。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-10 — P2 — 修复版本的 checkpoint/启动语义未定义

- **文件：** Task 5/6、`backend/services/workflow_publisher.py`、`backend/backends/n8n/gateway.py`。
- **风险：** 新版本发布后，失败中的旧 execution 是从失败节点恢复、从 webhook 重新开始，还是新建 execution 未定义；输入幂等键和 side effect 重复风险未解决。
- **修复：** 冻结恢复规则、checkpoint 迁移、幂等键保持/派生规则、旧 execution 的 retire/保留规则。
- **验收：** 修复版本只恢复一次；旧版本 execution 不重复副作用；历史版本和 checkpoint 可审计恢复。
- **回归证据：** migration v9 与 `tests/services/test_workflow_repair_coordinator.py::test_repaired_execution_resumes_once_from_durable_checkpoint_after_restart` 验证 checkpoint 恢复、稳定派生幂等键、parent/repaired execution 关系、旧 execution 的 `superseded_for_repair` 状态及重启重复启动抑制。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-11 — P2 — preflight capability 失败没有进入 escalation 流程

- **文件：** `backend/services/planning_job_runner.py`、`backend/blueprint/preflight.py`、Task 4/6/7。
- **风险：** `CAPABILITY_UNAVAILABLE` 等错误发生在 workflow 创建前，当前只会把 planning job 标记 failed；runtime classifier 无法接收到该事件，因此不会产生 Planning Agent escalation fact。
- **修复：** 将 planning-time preflight failure 映射到同一个 `workflow-escalation.v1` contract；不创建 workflow，持久化 reason/evidence，并通过独立 escalation outbox 投递。
- **验收：** 缺失能力、缺失 evaluator metric、backend feature 不可用分别生成确定的 escalation；不会生成 speculative workflow。
- **回归证据：** `tests/services/test_planning_preflight_escalation.py` 参数化验证 `CAPABILITY_UNAVAILABLE`、`EVALUATOR_METRIC_UNAVAILABLE`、`BACKEND_FEATURE_UNAVAILABLE` 均创建确定且幂等的 escalation outbox，不调用 reasoner 或 publisher。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-12 — P2 — v2 Canary policy 未定义且当前硬编码关闭

- **文件：** `backend/services/planning_job_runner.py:149`、`backend/contracts/execution_ir_v2.py`、Task 5/10。
- **风险：** v2 当前始终 `canary=False`，IR 没有 run_mode/canary 字段；计划要求修复版本经过 Canary，却没有规定初次版本、修复版本和失败时的确定性规则。
- **修复：** 明确 canary policy 的来源、默认值、IR/artifact 传播和失败状态；至少修复版本必须 Canary 通过后才激活。
- **验收：** repaired workflow Canary 失败时旧版本仍 active；没有通过 Canary 的版本不能 start；策略在重启后保持一致。
- **回归证据：** `tests/ir/test_execution_ir_models.py` 验证初始版本默认 `disabled`、所有 repaired workflow 强制 `required`；compiler artifact 持久化该策略，`tests/services/test_durable_publisher.py` 验证 Canary 未通过不激活/启动且旧版本保持 active，E2E repaired workflow 实际执行 Canary。全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-13 — P2 — manifest/README/Compose 仍描述旧 relay 语义

- **文件：** `manifest.json`、`README.md`、`docs/external-agent-contracts-v2.md`、`.env.example`、`docker-compose.yml`、Task 9。
- **风险：** manifest 仍列 `business_directives`；README 仍写“非技术失败 relay 到 planning agent”；Compose/.env 仍暴露 `ORCHESTRATOR_EVALUATOR_BASE_URL`，与 evaluator direct-to-planner 和 Orchestrator 完全分离边界不一致。
- **修复：** 一次性更新 manifest failure handling、README、external contract、Compose/env 和内容测试；删除不再属于 Orchestrator 的 evaluator callback 配置，或明确其仅为外部 evaluator-owned 配置。文档必须区分 evaluator direct channel 与 monitoring-absent 的 Orchestrator final-summary fallback。
- **验收：** 文档、manifest、compose、schema、source-map 对 channel ownership 一致；grep/content tests 不再出现旧 relay 语义，且明确 final summary 不是 evaluator relay。
- **回归证据：** `tests/test_development_bundle_contents.py::test_evaluator_and_orchestrator_channel_ownership_is_consistent` 验证 README、external contract、manifest、`.env.example`、Compose 和 adapter 不再包含 Orchestrator evaluator base URL/relay，并明确 direct evaluator 与 fallback summary 的不同所有权；全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

#### F-14 — P2 regression（本次审查修订引入）— source-map 与审查 plan 不一致

- **文件：** `docs/plans/2026-08-19-evaluator-orchestrator-separation.md`、`source-map.json`、`scripts/generate-development-artifacts.sh`。
- **依据：** 制品生成脚本会递归收集 `docs/` 文件并写入 `source-map.json`；本次封版审查新增/修改了该 plan，但现有 `source-map.json` 和 `artifacts/SHA256SUMS` 未包含该文件的当前 SHA-256。
- **风险：** 归档制品的源文件索引无法证明冻结 plan 与实际随包文档一致，迁移到另一台机器后审查记录可能无法复现。
- **修复：** 在任何重新生成开发制品前，运行 `scripts/generate-development-artifacts.sh`，使 source-map、SBOM/manifest 引用和 `artifacts/SHA256SUMS` 同步；随后运行 `scripts/verify-package.sh` 和归档内容测试。不得手工伪造 hash。
- **验收：** `source-map.json` 包含本文件路径及与 `sha256sum` 相同的值；`artifacts/SHA256SUMS` 校验通过；重新打包后的归档包含该 plan 且内容测试通过。
- **回归证据：** `scripts/generate-development-artifacts.sh` 离线生成开发镜像、SBOM、source-map 与 hashes 成功；随后 `scripts/verify-package.sh` 和 `sha256sum -c artifacts/SHA256SUMS` 均通过。计划状态更新后会再次执行同一制品门禁，使本回归证据和最终状态进入 source-map。
- **状态：** closed。

#### F-15 — P1 regression（monitoring-absent 修订引入）— fallback 的监控判定与终态信号未映射到现有 v2 运行时

- **文件：** `docs/plans/2026-08-19-evaluator-orchestrator-separation.md:9,24,68,98,131,266,275-324`、`backend/contracts/execution_ir_v2.py:56-57`、`backend/planning/execution_planner.py:103-163`、`backend/backends/n8n/compiler_v2.py:46,105-135`、`backend/contracts/runtime_event.py:30`。
- **依据：** 本次修订使用“监控节点”作为 fallback 判定，但现有 IR 只有 `evaluate`，没有 monitoring node 类型或编译标记；现有 Registry 只定义 `role=evaluator`。更关键的是，`evaluation_enabled=false` 时 compiler 会跳过所有通向 `end` 的边，而 `RuntimeEventV2` 只有 node attempt/checkpoint 事件，没有 workflow terminal event。无 evaluator 的实际产物因此既无可判定的 monitoring-node count，也没有可靠时机创建 final-summary outbox。
- **风险：** 无监控智能体的工作流可能悬空、无法到达终态，或完全不发送 Plan 智能体摘要；不同实现者可能把任意 `evaluate` 节点、final evaluator 节点或普通 runtime callback 解释为“监控节点”，造成不一致投递或 evaluator 边界泄漏。
- **修复：** 冻结术语和可恢复数据模型：将“监控节点”定义为由 compiler 标记的 direct-evaluator node/final-evaluator node，不能从运行时 payload 推断；将 `monitoring_mode` 和该确定性 count 持久化到 execution plan。无论 evaluator 是否存在，都编译一个独立的 Orchestrator terminal telemetry callback，并增加严格的 terminal runtime event/schema 与状态转换；该 callback 只报告执行事实，负责原子创建一次 final-summary outbox。不得将 evaluator HTTP 成功/失败作为 fallback 切换信号。
- **验收：** 以不存在 evaluator binding 的 registry 编译并执行：workflow 图仍可到达终态，exactly one terminal runtime event 生成 exactly one `workflow-final-summary.v1` outbox；带 evaluator binding 且有编译标记的 monitoring node 时不生成 fallback；重复 terminal callback、重启恢复和 evaluator HTTP 失败均不改变已持久化 monitoring mode；payload/数据库中不出现 evaluator opinion。
- **回归证据：** `tests/backends/test_n8n_v2_compiler.py` 验证 compiler-marked monitoring 节点、无 evaluator 图可达独立严格 terminal callback；migration v10 持久化 terminal event；`tests/api/test_runtime_events.py` 验证 fallback terminal callback 在重复、重启和 outbox retry 后只创建一个 summary，direct 模式在 evaluator HTTP 失败和重启后始终不创建 fallback；`tests/e2e/test_v2_pipeline_with_doubles.py` 验证 runner 只采用 compiler metadata 并持久化 mode/count/key。F-15 聚焦组合为 `25 passed`，全量回归为 `201 passed, 1 skipped`。
- **状态：** closed。

### Finding 状态规则

本节写入后 finding 集合冻结为 F-01 至 F-15。不得在后续实现过程中新增架构 finding；只有修订引入的 regression 或本次外部条件变化可以作为补充记录，并必须说明依据。修复后只做回归复审，逐项将状态从 `open` 更新为 `closed` 或 `regression`。
