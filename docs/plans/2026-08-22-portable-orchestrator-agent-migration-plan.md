# Portable Orchestrator and Agent Stack Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce a self-contained, offline-capable migration bundle that carries the Orchestrator, Agent Registry stack, n8n gateway adapter, Planning Agent test substitute, all runtime images, test data, startup scripts, and migration documentation to another machine without hard-coded workstation paths or host-side Python/pytest dependencies.

**Architecture:** Keep the existing two deployment boundaries: Agent Registry Compose owns Postgres, n8n, and registered business Agent services; Orchestrator Compose owns the Orchestrator runtime. Add a migration-level Compose layer for the Planning Agent test substitute and orchestrator-gateway.v1 adapter, then expose one portable top-level launcher that validates the host, loads immutable image archives, creates a shared Docker network, starts the two stacks in readiness order, and runs contract probes. All paths resolve from the extracted migration root, all service-to-service addresses use Compose DNS names, and all secrets remain runtime inputs outside image layers.

**Tech Stack:** Docker Engine, Docker Compose v2, offline Docker image archives, POSIX shell, Python 3 inside containers, FastAPI/Uvicorn services already present in the images, n8n, Postgres, SHA256 manifests, JSON contracts, pytest for package tests.

---

## Migration invariants

1. Every path in runtime code, Compose files, manifests, scripts, and migration documentation is relative to the extracted bundle root or supplied through an environment variable. The migration bundle contains no workstation-specific absolute path, fixed container name, fixed Docker project name, or fixed private IP.
2. The protected offline source bundle remains read-only and is never copied into the runtime image or modified by the migration process. Build provenance stores component identifiers and content hashes instead of source workstation paths.
3. The migration archive contains final runnable images for every service. A target machine does not run pip install, npm install, apt install, or any network build step.
4. The target machine requires Docker Engine, Docker Compose v2, a POSIX shell, tar, sha256sum, and basic text utilities. The bundle provides a detector that reports missing host capabilities before any state-changing start operation.
5. DeepSeek remains an external runtime endpoint when the Workflow Reasoner uses it. Its API key is created on the target machine as a secret file and never enters .env, an image layer, a checksum manifest, or a log.
6. Planning Agent evaluator separation remains unchanged. The test substitute accepts ordinary JSON, records the last request, and returns HTTP 202 accepted; it does not inject evaluator opinion into Orchestrator runtime facts or repair payloads.
7. The migration launcher is the only documented entry point for a new machine. Individual Compose commands remain available for diagnosis and recovery.

## Portable bundle layout

The build step emits this relative layout inside the migration archive:

```
portable-migration/
├── orchestrator/
├── agent-registry/
├── services/planning-agent/
├── services/orchestrator-gateway/
├── images/archives/
├── data/filter-library/
├── data/wordagent/input/
├── data/wordagent/merged/
├── data/wordagent/output/
├── data/pytestproject/
├── data/reportagent/
├── state/
├── logs/
├── config/
├── scripts/
├── docker-compose.migration.yml
├── migration-manifest.json
└── docs/MIGRATION.md
```

## Task 1: Establish the portability contract and path scanner

**Files:**
- Create: scripts/check-portable-paths.py
- Modify: contracts.json in the Agent Registry project
- Modify: scripts/preflight.py in the Agent Registry project
- Modify: scripts/source-platform-guard.py in the Agent Registry project
- Modify: scripts/orchestrator_gateway_dev.py
- Modify: package verifier handling for workspace metadata
- Test: tests/test_package_contract.py
- Test: tests/test_image_manifest.py
- Create: tests/test_portable_paths.py

**Step 1: Write failing tests**

Scan a synthetic extracted bundle and assert that runtime files contain no workstation absolute paths, Compose bind mounts are relative or named volumes, contracts store source identifiers and hashes instead of local source roots, gateway registry configuration resolves from environment or bundle-relative defaults, and all image archive names are relative.

Run:

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/test_portable_paths.py tests/test_package_contract.py tests/test_image_manifest.py
```

Expected: FAIL because current contracts, image provenance, workspace metadata, and gateway defaults contain workstation paths.

**Step 2: Implement portable path resolution**

Make every launcher derive its root from its own script location. Replace source_platform_root with protected_source_id, protected_source_markers, and source_content_sha256. The source guard accepts an optional SOURCE_PLATFORM_ROOT supplied by the build host; runtime startup does not require the protected source tree.

Inject registry file, n8n service, Postgres service, gateway URL, Orchestrator URL, and project identity through Compose. Remove fixed container names, private IPs, and workstation-specific registry paths.

**Step 3: Exclude non-portable metadata**

Exclude workspace metadata, historical evidence containing local paths, old checksum sidecars with absolute filenames, caches, runtime databases, and local secret directories from the runtime archive. Keep source-map entries only when they are bundle-relative and hash-based.

**Step 4: Verify**

Run:

```
python3 scripts/check-portable-paths.py --root .
```

Expected: zero workstation-path violations and zero secret-like matches.

## Task 2: Normalize Agent Registry Compose for relocation

**Files:**
- Modify: compose.yaml in the Agent Registry project
- Modify: .env.example in the Agent Registry project
- Modify: contracts.json
- Modify: scripts/preflight.py
- Modify: scripts/start.sh
- Modify: scripts/stop.sh
- Test: tests/test_preflight.py
- Test: tests/test_package_contract.py

**Step 1: Write relocation tests**

Run preflight from a temporary extracted directory and assert that all bind mounts stay below the extracted root, startup works from a different current directory, service names are used for internal URLs, missing data directories are created, and the shared network can be created on demand.

**Step 2: Remove network assumptions**

Make the network name configurable through REGISTRY_NETWORK_NAME. The top-level launcher creates it before either Compose project starts. Use Compose service names for n8n, Postgres, Word Agent, Pytest Agent, Report Agent, and Filter Agent URLs.

**Step 3: Make data and logs explicit**

Create these directories during startup:

```
data/filter-library
data/wordagent/input
data/wordagent/merged
data/wordagent/output
data/pytestproject
data/reportagent
logs/filter-library
logs/wordagent
logs/pytestproject
logs/reportagent
```

Keep generated Word, Pytest, and Report artifacts in these relative directories.

**Step 4: Verify**

Run:

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/test_preflight.py tests/test_package_contract.py tests/test_image_manifest.py
```

Expected: all tests pass with the source guard active and the protected source unchanged.

## Task 3: Make the Orchestrator Compose stack bundle-relative

**Files:**
- Modify: docker-compose.yml
- Modify: .env.example
- Modify: scripts/start.sh
- Modify: scripts/stop.sh
- Modify: scripts/verify-external-dependencies.sh
- Modify: scripts/verify-package.sh
- Modify: manifest.json
- Test: tests/test_top_level_launcher.py
- Test: tests/test_development_bundle_contents.py
- Create: tests/test_compose_portability.py

**Step 1: Write failing Compose portability tests**

Assert that secret file references are relative or explicitly configured, the external network name is supplied by an environment variable, no fixed n8n IP or workstation path appears in Compose, the Orchestrator uses migration network aliases, and the image reference is loaded locally with no pull.

**Step 2: Inject service endpoints through Compose**

Add these internal container contract values:

```
ORCHESTRATOR_PLANNING_AGENT_BASE_URL=http://planning-agent:18000
N8N_BASE_URL=http://n8n:5678
N8N_GATEWAY_BASE_URL=http://orchestrator-gateway:8080
REGISTRY_FILE=/bundle/agent-registry/registration/agents.json
```

The container paths are image/mount paths, not host paths. The host-side .env contains only relative secret references and configurable ports.

**Step 3: Keep image-only startup**

Change scripts/start.sh so it verifies the migration image manifest and loads images before docker compose up -d --no-build. Retain build-image-offline.sh for development rebuilds, but make the target-machine path use final image archives and never invoke a network build.

**Step 4: Verify**

Run:

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/test_top_level_launcher.py tests/test_development_bundle_contents.py tests/test_compose_portability.py
```

Expected: all package tests pass and the startup path contains no absolute workstation path.

## Task 4: Containerize and package the Planning Agent test substitute

**Files:**
- Create: services/planning-agent/app/planning_agent_stub.py
- Create: services/planning-agent/Dockerfile
- Create: services/planning-agent/README.md
- Modify: docker-compose.migration.yml
- Modify: manifest.json
- Test: tests/services/test_planning_agent_stub.py
- Test: tests/test_migration_manifest.py

**Step 1: Write contract tests**

Cover health 200, ordinary JSON POST returning HTTP 202 and accepted=true, atomic persistence to state/planning-agent-last.json, GET last, bounded request size, and configurable port.

**Step 2: Implement the service**

Use the current standard-library stub behavior as the baseline. Add state-file configuration, atomic replacement, bounded request size, structured logging without secrets, an explicit development HMAC bypass flag, and a writable state volume.

**Step 3: Build an offline image**

Use a locally available Python runtime base image. Install no packages. Copy only the stub and healthcheck metadata, expose port 18000, and set an immutable source-content label.

**Step 4: Add readiness ordering**

Make Orchestrator depend on Planning Agent health. The top-level launcher probes health and posts a harmless JSON request before starting Orchestrator.

**Step 5: Verify**

Run:

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/services/test_planning_agent_stub.py tests/test_migration_manifest.py
```

Expected: all Planning Agent contract tests pass and its image is listed in the migration manifest.

## Task 5: Containerize the compatible n8n gateway adapter

**Files:**
- Create: services/orchestrator-gateway/Dockerfile
- Create: services/orchestrator-gateway/entrypoint.sh
- Copy/modify: services/orchestrator-gateway/app/orchestrator_gateway_dev.py
- Modify: scripts/orchestrator_gateway_dev.py
- Modify: docker-compose.migration.yml
- Modify: schemas/external/n8n-gateway-capabilities.v1.json
- Test: tests/services/test_orchestrator_gateway_dev.py
- Test: tests/contracts/test_n8n_gateway_contract.py

**Step 1: Write gateway contract tests**

Validate every required gateway operation:

```
GET  /api/v2/orchestrator/capabilities
GET  /api/v2/orchestrator/registry
POST /api/v2/orchestrator/workflows/validate
POST /api/v2/orchestrator/workflows
POST /api/v2/orchestrator/workflows/{id}/canary
POST /api/v2/orchestrator/workflows/{id}/activate
POST /api/v2/orchestrator/workflows/{id}/start
POST /api/v2/orchestrator/executions/{id}/resume
GET  /api/v2/orchestrator/executions/{id}
POST /api/v2/orchestrator/workflows/{id}/retire
GET  /api/v2/orchestrator/workflows/{id}/live-references
POST /api/v2/orchestrator/runtime-events
GET  /api/v2/orchestrator/operations/{key}
```

Validate the complete n8n-gateway-capabilities.v1 document and every required operation version.

**Step 2: Remove fixed runtime identities**

Resolve n8n, Postgres, registry file, gateway port, and Orchestrator URL from Compose variables. Use service DNS names. Generate workflow and operation identities from canonical payload hashes or UUIDs instead of a fixed n8n project ID.

**Step 3: Package gateway dependencies**

The current adapter invokes Docker CLI operations to import and publish n8n artifacts. Package the gateway as an offline image containing its Docker API client and mount the Docker socket with the narrowest available access policy. Inject the n8n service and Postgres service names through Compose instead of discovering fixed container names. Export the gateway image, client, and Python modules into the image archive, and document the socket requirement as a Docker-only host capability.

**Step 4: Preserve security boundaries**

Keep HMAC signing for runtime events, evaluator direct routing, and recursive rejection of evaluator-owned fields. The adapter remains a compatibility implementation of orchestrator-gateway.v1.

**Step 5: Verify**

Run:

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/services/test_orchestrator_gateway_dev.py tests/contracts/test_n8n_gateway_contract.py
```

Expected: capabilities, validate, lifecycle, runtime-event, idempotency, and evaluator-field rejection tests pass.

## Task 6: Build the complete offline image inventory

**Files:**
- Modify: images/manifest.json in the Agent Registry project
- Modify: images/SHA256SUMS
- Modify: artifacts/image-manifest.json
- Modify: artifacts/SHA256SUMS
- Modify: scripts/export-images.sh
- Modify: scripts/load-images.sh
- Create: scripts/export-migration-images.sh
- Create: scripts/load-migration-images.sh
- Create: tests/test_migration_manifest.py

**Step 1: Define required final images**

List final images for Postgres, n8n, Filter API/UI, Word Agent, Pytest Agent, Report Agent, Orchestrator, Planning Agent stub, and gateway. Each entry records tag, image ID, architecture, operating system, relative archive path, byte size, and SHA256.

**Step 2: Export final images**

Use docker save for every final tag. Parent layers remain in each archive, so the target machine does not download or rebuild base images. The exporter fails if any final image is absent and never calls a registry.

**Step 3: Verify and load**

load-migration-images.sh verifies the image checksum manifest, validates the manifest schema and required set, loads every archive, inspects image IDs and architecture, and fails on mismatch.

**Step 4: Scan layers**

Extend the layer scanner to reject private keys, API keys, .env files, caches, source-reference paths, and package manager caches while permitting runtime libraries, pytest, templates, fonts, browser binaries, and other baked-in dependencies.

**Step 5: Verify**

Run:

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/test_migration_manifest.py tests/test_image_manifest.py
```

Expected: every manifest entry has a matching archive, checksum, and locally inspectable image.

## Task 7: Create migration Compose and shared network

**Files:**
- Create: docker-compose.migration.yml
- Modify: docker-compose.yml
- Modify: Agent Registry compose.yaml
- Create: config/.env.example
- Create: migration-manifest.json
- Test: tests/test_compose_portability.py

**Step 1: Define services and aliases**

The migration Compose file adds planning-agent, orchestrator-gateway, and the Orchestrator service. Agent Registry Compose continues to own Postgres, n8n, and business Agent containers. Both projects join a configurable shared network with stable aliases for every service.

**Step 2: Encode readiness order**

Use health conditions in this order:

```
postgres healthy
  → n8n healthy
  → Registry provisioning complete
  → planning-agent healthy
  → gateway capabilities ready
  → orchestrator healthy
```

Agent data volumes are relative bind mounts. Orchestrator SQLite and Planning Agent state use volumes under state/.

**Step 3: Test relocated rendering**

Copy the assembled bundle to a directory with a different name, run Compose config rendering from a different current directory, and assert that every host bind mount stays below the extracted root.

Expected: Compose config succeeds, all mounts are bundle-relative, and no workstation-specific external network is required.

## Task 8: Add host prerequisite and secret preparation scripts

**Files:**
- Create: scripts/check-host-prerequisites.sh
- Create: scripts/prepare-runtime-secrets.sh
- Create: config/secrets.example/README.md
- Modify: config/.env.example
- Test: tests/test_host_prerequisites.py
- Test: tests/test_secret_handling.py

**Step 1: Detect host prerequisites without Python**

Check docker info, Docker Compose v2, architecture compatibility, tar, sha256sum, awk, sed, grep, mktemp, curl, disk space, required ports, bridge-network creation, disposable image loading, bind-mount support, and executable scripts. Print human-readable remediation and machine-readable JSON. Stop before application state changes when a required capability is missing.

**Step 2: Prepare runtime secrets**

Create relative .runtime-secrets files for n8n API, DeepSeek API, n8n webhook path/auth, Planning HMAC, and n8n HMAC. Set restrictive permissions, refuse accidental overwrite, and scan rendered configuration and image layers for secret leakage.

**Step 3: Verify failure handling**

Mock Docker and temporary directories to cover missing Docker, missing Compose v2, insufficient disk, occupied ports, missing secret, unreadable secret, and architecture mismatch.

## Task 9: Implement the single migration launcher

**Files:**
- Create: scripts/start-all.sh
- Create: scripts/stop-all.sh
- Create: scripts/status-all.sh
- Create: scripts/migrate-state.sh
- Modify: scripts/start.sh
- Modify: Agent Registry start.sh and stop.sh
- Test: tests/test_start_all.py
- Test: tests/test_state_migration.py

**Step 1: Define idempotent startup phases**

```
1. resolve bundle root from the script location
2. run host prerequisite checks
3. create data, log, state, and secret directories
4. verify migration and image checksum manifests
5. load final images
6. create or reuse the shared network
7. start Postgres and wait for readiness
8. start n8n and wait for healthz
9. provision n8n workflows and credentials
10. start Agent Registry services and register agents
11. start Planning Agent and POST a probe JSON
12. start gateway and verify capabilities
13. start Orchestrator and verify health
14. print container status, image IDs, endpoints, state files, and logs
```

Each phase has a bounded timeout and reports the exact failing container and next diagnostic command.

**Step 2: Implement stop, status, and state migration**

Stop Orchestrator first, then gateway and Planning Agent, then business Agents, n8n, and Postgres. Preserve data, state, and logs by default. Export/import approved relative directories and named volumes through a manifest that rejects path traversal and keeps secrets separate.

**Step 3: Verify idempotency**

Run twice with a mocked Docker CLI and once against a disposable local Docker network. The second run reuses healthy services, creates no duplicate registrations, keeps Planning Agent state readable, and preserves artifacts across restart.

## Task 10: Assemble the migration archive

**Files:**
- Create: scripts/build-migration-bundle.sh
- Modify: scripts/package-development-bundle.sh
- Modify: scripts/verify-package.sh
- Create: tests/test_migration_archive.py
- Create: docs/MIGRATION.md

**Step 1: Define bundle-relative inputs**

Accept orchestrator directory, Agent Registry directory, Agent image directory, and output directory as relative CLI arguments. Copy only approved runtime files, registration/workflow definitions, data fixtures, image archives, service source, schemas, scripts, and documentation. The protected offline source is never an output member.

**Step 2: Generate migration-manifest.json**

Record schema/version, component hashes, image tags/IDs/architectures/archive hashes, Compose files and services, host capabilities, runtime secret names, host ports, data directories, artifact contracts, startup order, readiness probes, and verification commands. Write no local source path, API key, private key, or hostname.

**Step 3: Build and verify**

Create a reproducible tar archive with sorted entries, normalized ownership, stable timestamps where practical, no caches, and no runtime databases unless exported through state migration. Write a SHA256 sidecar and extract into a temporary directory for a relocation smoke test.

Run:

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/test_migration_archive.py tests/test_migration_manifest.py tests/test_portable_paths.py
```

Expected: extraction succeeds, hashes match, the path scanner finds no local absolute path, and no secret-like content is present.

## Task 11: Write the new-machine migration guide

**Files:**
- Create: docs/MIGRATION.md
- Modify: README.md
- Modify: Agent Registry README.zh-CN.md
- Test: tests/test_migration_docs.py

The guide must use only commands relative to the extracted bundle:

### Target machine command path

```
tar -xzf portable-migration.tar.gz
cd portable-migration
./scripts/check-host-prerequisites.sh
./scripts/prepare-runtime-secrets.sh
cp config/.env.example .env
./scripts/start-all.sh
./scripts/status-all.sh
```

Document every container, purpose, readiness probe, host port, data directory, log directory, and recovery command. Explain that the Planning Agent test substitute is packaged as a container, persists the last JSON under state/, exposes health and last-request endpoints, and is started before Orchestrator. Explain that gateway capabilities must pass before Orchestrator starts.

## Task 12: Execute end-to-end offline acceptance

**Files:**
- Create: tests/e2e/test_portable_migration.py
- Modify: scripts/verify-package.sh
- Modify: scripts/verify-v2-regression.sh

**Step 1: Clean extraction**

Extract the archive into a directory with a different name and run from a different current working directory. Do not copy any source tree or install application dependencies.

**Step 2: Start and verify**

Run:

```
./scripts/check-host-prerequisites.sh
./scripts/verify-migration-bundle.sh
./scripts/load-migration-images.sh
./scripts/start-all.sh
./scripts/status-all.sh
```

Expected: every required image loads locally, every service is healthy, and no pull/build/install command is issued.

**Step 3: Verify Planning Agent**

POST a harmless JSON envelope, confirm HTTP 202 accepted, inspect the last-request endpoint, restart the service, and confirm the state file still contains the request.

**Step 4: Verify the real Orchestrator path**

Submit a blueprint through the Orchestrator v2 API and collect planning job ID, gateway workflow ID, validate/canary/activate/start results, execution ID, runtime terminal event, final-summary outbox operation key, Planning Agent last JSON, actual Agent outputs, and Report artifact paths. Prove that the request used the gateway API and not a hand-authored webhook.

**Step 5: Run final verification**

```
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider
sha256sum -c artifacts/SHA256SUMS
scripts/verify-package.sh
```

## Task 13: Handoff checklist

- [ ] no runtime or documentation file in the migration archive contains a local absolute path;
- [ ] the protected offline source bundle remains unchanged;
- [ ] every final image archive is checksum-verified;
- [ ] pytest is available inside the Pytest Agent image;
- [ ] Word output directories exist and are writable;
- [ ] Report Agent reads migrated Word/Pytest artifacts;
- [ ] Planning Agent is packaged, starts automatically, persists the last JSON, and exposes health;
- [ ] gateway capabilities return the complete n8n-gateway-capabilities.v1 contract;
- [ ] Orchestrator starts only after gateway and Planning Agent readiness;
- [ ] start-all.sh works from an arbitrary current directory;
- [ ] stop/start preserves approved data and state;
- [ ] secrets remain outside archives and image layers;
- [ ] the migration guide identifies every script, Compose file, container, probe, and recovery command.

## Execution handoff

Execute this plan with superpowers:executing-plans task-by-task. Keep each task independently testable and commit after each verified milestone when the target workspace has version control enabled.

## 封版审查记录（冻结基线）

本节在需求冻结后加入，作为本计划的唯一 P0–P3 问题清单。审查范围覆盖本计划、真实代码、Compose 与镜像清单、签名材料、数据库状态迁移设计和现有测试。审查期间不扩展 evaluator/Orchestrator 架构，不增加新的功能编号；后续只验证本节修复及其回归。

审查结论：P0=0，P1=6，P2=2，P3=0。Planning Agent 测试替身已纳入迁移范围；宿主机上正在运行的临时 Python 进程不计作迁移完成证据。两条并行审查线发生工具传输超时，未产生可采纳的新 finding；该外部条件已记录，后续只在审查工具恢复后补充同范围复核。

### F-01（P1）测试收集目录发生模块名冲突

- 文件：`agent-registry-full-stack-dev-20260819/data/pytestproject/test_login.py`、`data/pytestproject/runtime/test_login.py`、pytest 配置及迁移验收脚本。
- 风险：全量 pytest 在 collection 阶段产生 `import file mismatch`，迁移包无法完成最终验收，且生成的 runtime 测试文件可能污染项目测试。
- 修复：将 runtime 目录设为运行时产物并从项目测试收集中排除；为迁移 fixture 使用唯一模块名；在 Compose/启动脚本中将运行时输入与源码测试目录分离；补充 `testpaths`/`norecursedirs` 或等效收集规则。
- 验收：在原目录和随机解压目录分别执行 `PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider`，collection 无冲突且全量通过；在执行 `/api/pytest/run` 后再次运行全量测试，结果保持一致。

### F-02（P1）Gateway 控制面缺少认证边界

- 文件：`scripts/orchestrator_gateway_dev.py`、Gateway Compose/启动配置。
- 风险：Gateway 绑定 `0.0.0.0`，工作流创建、激活、启动、退休等生命周期操作可被未认证调用者直接执行。
- 修复：为控制面定义并实现服务间认证（API key 或 HMAC）；仅允许 Orchestrator 服务网络访问控制路由；对外暴露时通过显式反向代理策略；Planning Agent 的开发期免 HMAC 仅适用于测试替身，不适用于 Gateway 控制面。
- 验收：未认证请求对全部控制路由返回 401/403；合法 Orchestrator 凭据可完成 validate→create→canary→activate→start→resume→retire；能力接口明确声明认证要求；网络检查确认控制端口不向非授权网络开放。

### F-03（P1）`live-references` 路由存在 SQL 注入路径

- 文件：`scripts/orchestrator_gateway_dev.py` 中 `/api/v2/orchestrator/workflows/{id}/live-references` 查询。
- 风险：workflow id 当前直接拼接进 SQL；构造路径可改变查询条件。Gateway 同时拥有 Docker、Postgres 和 n8n 控制权限，影响面扩大。
- 修复：统一采用 `[A-Za-z0-9_-]+` workflow id 白名单；查询使用参数化 SQL（或等效安全查询接口）；拒绝编码后的引号、空格、分号和控制字符。
- 验收：合法 id 返回 live-reference 结果；注入样例返回 400，不触发 SQL 错误或额外行；回归测试覆盖 URL 解码前后两种输入。

### F-04（P1）SQLite/Postgres 状态迁移缺少一致性备份与回滚协议

- 文件：`scripts/migrate-state.sh`、Orchestrator SQLite 状态目录、n8n/Postgres 状态迁移任务。
- 风险：直接归档运行中的 WAL 数据库或 Postgres volume 可能产生不一致快照，恢复后丢失 workflow、execution 或 operation 状态。
- 修复：迁移前停止或 quiesce 写入；SQLite 使用 backup API，显式处理 WAL/SHM 并执行 `PRAGMA integrity_check`；Postgres 使用 `pg_dump`/`pg_restore`；记录 row count 与关键 invariant；失败时保留原状态并提供回滚步骤。
- 验收：备份、恢复、完整性检查和 row-count 对账全部通过；故意中断恢复步骤时原状态可启动且校验和不变；不得以正在运行的数据库 volume tar 作为唯一备份证据。

### F-05（P1）运行时归档可能携带不可迁移的离线源元数据

- 文件：`agent-registry-full-stack-dev-20260819/data/source-before.json`、`contracts.json`、`build/source-allowlists.json`、`build-contexts/selected-source-manifest.json` 及相关 provenance 测试。
- 风险：`source-before.json` 含离线 bundle 的本地源路径；复制 data 或 provenance 文件会把当前机器路径带入迁移包，破坏可迁移性并泄露环境信息。
- 修复：定义 runtime archive allowlist；排除 `data/source-before.json`、缓存、历史证据和本地 source allowlist；provenance 仅保留 bundle-relative 标识、内容哈希和版本字段；归档后扫描 JSON、文本、Compose、镜像标签和 layer 元数据。
- 验收：随机目录解压后路径扫描覆盖全部归档文件与镜像元数据；不出现宿主机特定的绝对路径模式、当前项目名、固定容器名或私网 IP；离线 bundle 的校验和保持原值。

### F-06（P1）Compose/Gateway 默认值绑定当前机器网络与容器命名

- 文件：Agent Registry `compose.yaml`、Orchestrator `.env`/`.env.example`、`scripts/orchestrator_gateway_dev.py`、启动脚本。
- 风险：当前配置出现固定 Compose project/container name 及 `172.20.0.x`、`172.17.0.1` 地址；换机或换解压目录后服务发现失败，且 external network 可能不存在。
- 修复：服务间通信统一使用 Compose service name；启动器创建并注入共享网络；n8n、Planning Agent、Gateway、Postgres 地址通过环境变量或内部 DNS 提供；移除固定容器名、项目名和宿主机 IP 默认值；保留 `.env.example` 的可迁移默认值。
- 验收：在两个不同目录执行 `docker compose config` 和完整启动；所有健康检查通过；配置渲染结果不含当前机器绝对路径、固定容器名或私网 IP；重建后 Orchestrator 仍能通过 Gateway 完成能力检查。

### F-07（P2）镜像架构仅覆盖 amd64，目标主机兼容性未闭合

- 文件：Agent/Orchestrator 镜像 manifest、镜像加载脚本、主机前置检查脚本。
- 风险：迁移到 arm64 主机时镜像无法运行；当前计划没有明确阻断、仿真或多架构策略。
- 修复：在打包 manifest 中声明支持的 `os/architecture`；提供 arm64 镜像或在前置检查中明确拒绝不匹配架构；仿真仅作为显式、受支持的部署选项，不作为默认假设。
- 验收：`check-host-prerequisites.sh` 在架构不匹配时给出可操作错误并以非零退出；匹配架构可离线加载并启动全部服务；manifest 与实际镜像 inspect 结果一致。

### F-08（P2）开发包只有 SHA256，缺少来源认证

- 文件：`artifacts/SHA256SUMS`、`images/SHA256SUMS`、开发包 manifest。
- 风险：SHA256 可发现传输损坏，但无法证明包来源或防止替换；当前 manifest 标记 `artifact_signing=false`。
- 修复：在文档和启动检查中明确这是 unsigned development bundle；校验和必须通过可信渠道单独分发；规划后续签名、公钥和信任根接入，签名启用前不宣称来源认证。
- 验收：迁移前 checksum 校验通过；缺少或篡改 checksum 时启动被阻断；验收报告明确记录“完整性已验证、来源认证未启用”。

### 修复顺序与回归复审

先关闭 F-01 至 F-06，再处理 F-07、F-08 的发布门槛。每项修复必须补充对应验收测试并更新本计划中的任务、manifest、Compose、脚本和迁移文档。修订完成后只运行回归复审：Orchestrator 全量 pytest、Agent Registry 全量 pytest、checksum 校验、`scripts/verify-package.sh`、Compose 配置渲染、数据库备份恢复验收和随机目录端到端启动。回归阶段允许新增的 finding 仅限于修订引入的回归，或首次因外部环境/工具不可用而无法验证的同范围条件，并须在 finding 中注明证据和原因。
