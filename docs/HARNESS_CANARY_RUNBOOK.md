# KiCad Design Multi-Agent System Harness Canary、Evolution Worker 与 Flyway 发布手册

本手册只描述 Harness Runtime 的版本化发布。浏览器、Java 控制面和数据库迁移保持各自独立的发布边界。所有示例都要求显式传入 Kubernetes context，避免误操作当前默认集群。

## 发布模型

KiCad Design Multi-Agent System 保留两条物理隔离的 Runtime 路径：

| Channel | API Service | Temporal Worker | Task queue |
|---|---|---|---|
| stable | `ratsnest-agent-service` | `ratsnest-temporal-worker` | `ratsnest-hardware-cell-01` |
| canary | `ratsnest-agent-service-canary` | `ratsnest-temporal-worker-canary` | `ratsnest-hardware-cell-01-canary` |

Canary 不加入 stable Service，也不通过副本比例随机分流。控制面必须在创建 Run 时显式选择 `http://ratsnest-agent-service-canary:8080`（或 gRPC `ratsnest-agent-service-canary:9090`），并把所选 `harness_version_id` 固定到 Run、Temporal input、checkpoint 和产物 Manifest。一次 Run 不得在执行过程中切换 Channel。

Kubernetes Deployment 保存最近 10 个 ReplicaSet revision。镜像必须使用 `repository@sha256:<digest>`，版本身份同时写入：

- `RATSNEST_HARNESS_VERSION_ID`
- `RATSNEST_HARNESS_CHANNEL`
- `RATSNEST_HARNESS_MANIFEST_DIGEST`
- Pod labels `ratsnest.io/harness-version`、`ratsnest.io/release-track`
- Pod annotations `ratsnest.io/runtime-image-digest`、`ratsnest.io/harness-manifest-digest`

仓库中的 `replace-me` 是模板哨兵，不能直接成为生产 desired state。发布记录必须保存实际 digest；在没有 GitOps 控制器的当前实现中，禁止在 promotion 后重新应用仍含占位值的 base，否则会覆盖 live Deployment 的已批准版本。

## 1. 静态检查

以下命令只渲染本地文件，不连接 Kubernetes API：

```powershell
kubectl kustomize deploy/k8s/overlays/harness-canary *> $null
& .\scripts\check_infrastructure.ps1
```

渲染后的 Canary 副本数为零，应用 overlay 本身不会承接请求或执行 Temporal Activity。

## 2. Flyway 迁移

Java 应用的 `spring.flyway.enabled=false` 是刻意设计：业务 Pod 不争抢数据库迁移。发布负责人使用和候选 Java 控制面相同的不可变镜像，单独执行 `FlywayMigrationMain`。

数据库角色必须分离：

- `ratsnest_migrator`：拥有 `control_plane` schema，只有迁移 Job 使用。
- `ratsnest_app`：Java 运行时身份，必须是 `NOSUPERUSER NOBYPASSRLS`，不得拥有 schema 或表。

先由 Secret 管理系统通过独立的 `ratsnest-flyway-secrets` 提供 `RATSNEST_FLYWAY_USER` 和 `RATSNEST_FLYWAY_PASSWORD`。禁止把 schema-owner 凭据放进会被 Java/Python Runtime 通过 `envFrom` 加载的 `ratsnest-runtime-secrets`。随后使用候选控制面镜像在客户端替换 Job image：

```powershell
$context = "production-primary"
$image = "ghcr.io/ratsnestteam/ratsnest-control-plane@sha256:<64-hex-digest>"
$rendered = kubectl set image `
  -f deploy/k8s/operations/flyway-migrate.yaml `
  flyway=$image `
  --local `
  -o yaml

# 若同名 Job 仍存在，先保存它的日志和发布证据，再删除该 Job 对象。
kubectl --context $context -n ratsnest delete job/ratsnest-flyway-migrate --ignore-not-found
$created = $rendered | kubectl --context $context create -f - -o json | ConvertFrom-Json
$job = $created.metadata.name
kubectl --context $context -n ratsnest wait --for=condition=complete "job/$job" --timeout=10m
kubectl --context $context -n ratsnest logs "job/$job"
```

成功日志必须包含 `Flyway migration completed`，且当前发布必须看到 V9、V10 与 V11 的 `success=true`；V10 保存 promote 前的可验证 stable 回滚目标，V11 保存 Trial 的不可变输入、权威评测证明及唯一 pending/workflow 约束。随后使用只读查询保存版本、描述、checksum 和成功状态作为发布证据：

```sql
SELECT installed_rank, version, description, checksum, installed_on, success
FROM control_plane.flyway_schema_history
ORDER BY installed_rank;
```

### Flyway 失败处理

- checksum 不一致时停止发布；禁止自动执行 `repair`。
- 迁移脚本一旦进入共享环境便不可修改，修正应使用新的 `V<N>__description.sql`。
- Deployment rollback 不会回滚数据库。数据库采用 expand/migrate/contract：先增加向后兼容结构，应用稳定后迁移数据，最后在独立版本删除旧结构。
- 已执行的破坏性迁移不能依赖 `kubectl rollout undo` 恢复，必须使用经过审查的 forward-fix；必要时从经过演练的备份恢复。
- 迁移失败时不要启动 Canary，也不要删除失败日志。

## 3. 安装隔离的 Canary 资源

```powershell
$context = "production-primary"
kubectl --context $context apply -k deploy/k8s/overlays/harness-canary
```

该 overlay 只包含 Canary Workload、Service 和增量 NetworkPolicy，不重新应用或覆盖 stable 资源。stable cell 必须已经存在，并提供 `ratsnest-runtime-config`、`ratsnest-runtime-secrets`、ServiceAccount 和 PVC。

检查两个 Canary Deployment 仍为零副本：

```powershell
kubectl --context $context -n ratsnest get deployment `
  ratsnest-agent-service-canary,ratsnest-temporal-worker-canary
```

## 4. 部署候选 Harness

`deploy_harness_canary.ps1` 只接受不可变镜像 digest。任一 Deployment 启动失败时，脚本会把两个 Canary Deployment 都缩回零副本。

```powershell
& .\scripts\deploy_harness_canary.ps1 `
  -Context $context `
  -VersionId "harness-1.4.0-rc.1" `
  -Image "ghcr.io/ratsnestteam/ratsnest-agent-service@sha256:<64-hex-digest>" `
  -ManifestDigest "<64-hex-harness-manifest-digest>"
```

只把内部 Eval 租户或明确选择的 Run 发往 `ratsnest-agent-service-canary`。至少验证：身份签名、Run 幂等、SSE 重连、Temporal 恢复、Artifact truth、Historical/Hidden/Adversarial Eval 和资源预算。

Kubernetes 就绪并不等于开始分流。候选版本必须先注册为 `APPROVED/CANARY`，再由经过 OIDC 认证且具有发布权限的控制面管理 API 以 CAS 更新 `harness_rollouts.canary_version_id`、`canary_percent` 和 `row_version`。脚本不会绕过控制面直接写 PostgreSQL；没有访问令牌时应停止并由发布管理员执行该 API。

以下是精确的控制面调用顺序。Manifest 字段必须来自 `build_harness_manifest.ps1 -Release` 和不可变镜像构建结果，不能手填伪摘要：

```powershell
$controlPlane = "https://control.kicad-design-multi-agent-system.example"
$token = "<platform-admin-access-token>"
$headers = @{ Authorization = "Bearer $token" }
$versionId = "harness-1.4.0-rc.1"

$register = @{
  harnessVersionId = $versionId
  version = "1.4.0-rc.1"
  parentVersionId = "legacy-baseline"
  sourceCommit = "<40-or-64-hex-commit>"
  sourceTreeDigest = "<64-hex-tree-digest>"
  dirty = $false
  runtimeImageDigest = "sha256:<64-hex-image-digest>"
  toolchainDigest = "sha256:<64-hex-toolchain-digest>"
  bundleDigest = "<64-hex-bundle-digest>"
  contractDigest = "<64-hex-contract-digest>"
  policyDigest = "<64-hex-policy-digest>"
  manifestObjectKey = "harness-manifests/$versionId.json"
  manifestDigest = "<64-hex-harness-manifest-digest>"
} | ConvertTo-Json

$candidate = Invoke-RestMethod -Method Post `
  -Uri "$controlPlane/api/v1/platform/harness-versions" `
  -Headers $headers -ContentType "application/json" -Body $register

$approve = @{
  expectedVersion = [long]$candidate.rowVersion
  targetStatus = "APPROVED"
  reason = "Eval, code review and immutable build evidence accepted"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "$controlPlane/api/v1/platform/harness-versions/$versionId`:transition" `
  -Headers $headers -ContentType "application/json" -Body $approve

$rollout = Invoke-RestMethod -Method Get `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production" `
  -Headers $headers
$canary = @{
  expectedVersion = [long]$rollout.rowVersion
  canaryVersionId = $versionId
  canaryPercent = 5
} | ConvertTo-Json

Invoke-RestMethod -Method Put `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production/canary" `
  -Headers $headers -ContentType "application/json" -Body $canary
```

任一 `409` 表示 CAS 或状态已改变，应重新读取 Version/Rollout 后人工判断；禁止修改 `expectedVersion` 盲重试。

## 5. 提升到 stable

确认 Canary API 与 Worker 均 Ready、使用相同镜像/版本/Manifest，并完成审批后执行：

1. 先通过控制面管理 API 把新 Run 临时切到 100% Canary，停止向 stable Runtime 创建新 Run。
2. 等待已创建的 stable 请求进入可安全滚动的状态。
3. 更新 stable Kubernetes Deployment：

```powershell
& .\scripts\promote_harness_canary.ps1 `
  -Context $context `
  -DatabaseTrafficDrainedToCanary
```

该脚本从运行中的 Canary 读取不可变身份，以单次 Pod-template patch 分别更新 stable API 和 Worker。任一 stable rollout 失败时，脚本会对已修改的 Deployment 发起 `rollout undo`。

4. stable Pod 验证通过后，再通过控制面管理 API 原子设置 `stable_version_id=<candidate>`、清空 Canary 并令 `canary_percent=0`。CAS 冲突必须重新读取，不得覆盖其他发布者。

对应的两次 CAS 调用如下；每次都重新 GET，不能复用旧 `rowVersion`：

```powershell
$rollout = Invoke-RestMethod -Method Get `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production" `
  -Headers $headers
$drain = @{
  expectedVersion = [long]$rollout.rowVersion
  canaryVersionId = $versionId
  canaryPercent = 100
} | ConvertTo-Json
Invoke-RestMethod -Method Put `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production/canary" `
  -Headers $headers -ContentType "application/json" -Body $drain

# 仅在 stable API/Worker 已换成同一版本且验证完成后调用。
$rollout = Invoke-RestMethod -Method Get `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production" `
  -Headers $headers
$promote = @{
  expectedVersion = [long]$rollout.rowVersion
  canaryVersionId = $versionId
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production`:promote" `
  -Headers $headers -ContentType "application/json" -Body $promote
```

提升成功后 Canary 不会自动缩容。先停止向 Canary 创建新 Run，确认 `ratsnest-hardware-cell-01-canary` 没有待处理或运行中的 Workflow，再执行：

```powershell
kubectl --context $context -n ratsnest scale `
  deployment/ratsnest-agent-service-canary `
  deployment/ratsnest-temporal-worker-canary `
  --replicas=0
```

这条 drain 规则防止缩容后遗留无法消费的 Canary Activity。

## 6. 显式回滚

先查看两个 Deployment 的历史和当前 Harness 身份：

```powershell
kubectl --context $context -n ratsnest rollout history deployment/ratsnest-agent-service
kubectl --context $context -n ratsnest rollout history deployment/ratsnest-temporal-worker
kubectl --context $context -n ratsnest get pods -l ratsnest.io/release-track=stable `
  -L ratsnest.io/harness-version
```

默认回到各自上一个 revision：

```powershell
& .\scripts\rollback_harness.ps1 `
  -Context $context `
  -Channel stable `
  -DatabaseTrafficDrained
```

只有确认 API 与 Worker 的目标 revision 同属于一个 Harness 版本时，才指定 revision：

```powershell
& .\scripts\rollback_harness.ps1 `
  -Context $context `
  -Channel stable `
  -Revision 7 `
  -DatabaseTrafficDrained
```

回滚前同样必须通过控制面管理 API 把新 Run 导向已确认健康的 Runtime；Kubernetes 回滚验证完成后，再以 CAS 把数据库 stable 指针切回对应版本。只回滚 Deployment 而不更新 `harness_rollouts` 会造成 Run 快照与实际代码不一致，属于发布失败。

先读取 rollout，响应中的 `previousStableVersionId` 是最近一次 promote 记录的唯一允许目标。它必须对应一个 attested、`RETIRED` 的 HarnessVersion；浏览器或管理员不能指定任意 retired 版本。随后提交以下精确请求体：

```powershell
$controlPlane = "https://control.kicad-design-multi-agent-system.example"
$token = "<platform-admin-access-token>"
$headers = @{ Authorization = "Bearer $token" }
$rollout = Invoke-RestMethod `
  -Method Get `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production" `
  -Headers $headers

$body = @{
  expectedVersion = [long]$rollout.rowVersion
  targetVersionId = $rollout.previousStableVersionId
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "$controlPlane/api/v1/platform/harness-rollouts/production`:rollback" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

等价 JSON 为：

```json
{
  "expectedVersion": 8,
  "targetVersionId": "harness-1.3.7"
}
```

CAS 成功后，控制面会在一个数据库事务中执行当前 stable → `ROLLED_BACK`、目标 previous stable → `STABLE`，并把 rollout 的 `previousStableVersionId`、`canaryVersionId` 清空及 `canaryPercent` 置零。若已有 active canary，先停止并清理该 canary；接口不会把“取消 canary”伪装成 stable rollback。

回滚完成后核对两个 Pod 的版本、镜像 digest、Manifest digest，以及新 Run 的持久化版本快照。已经启动的 Run 仍保留原版本身份，不能通过数据库更新强制改版。

## 7. Temporal 兼容性边界

Canary task queue 隔离了候选运行。stable Deployment 的滚动更新仍要求 Workflow 代码保持 replay-compatible。会改变 Workflow command history 的发布必须使用 Temporal Worker Versioning/Patching 或先 drain 对应 Workflow；仅靠 Kubernetes rollback 不能修复 Temporal nondeterminism。普通 Activity 实现与 Prompt/Policy 变更仍可使用本手册的 Deployment 流程。

## 8. 发布证据

每次发布至少保存：

- Git commit 与 Harness Manifest digest
- stable/canary OCI image digest
- Flyway schema history 查询与 Job 日志
- Eval 报告和人工审批记录
- Deployment revision 与 rollout 状态
- Canary 指标窗口
- promote/rollback 操作者、时间和 request/trace ID

Kubernetes 提供的是可执行版本切换和 ReplicaSet 回滚；PostgreSQL/Flyway、Temporal history 和 S3 Artifact 分别保持自己的不可变审计链，不能用 Deployment 状态替代这些证据。

## 9. 显式启用生产 Evolution 执行面

该 overlay 默认创建两个 `replicas: 0` 的独立信任域，绝不随产品启动：

- `ratsnest-evolution-controller` 是受信控制器。它运行 Workflow、生成/签署证明并回调控制面，持有唯一的内部签名密钥；它没有 Kubernetes Token、Git/PVC 挂载，也不执行候选代码。
- `ratsnest-evolution-sandbox-coordinator` 只运行固定评测 Activity。它没有业务、模型、数据库或签名密钥；其 namespace Role 只能管理临时 Job、ConfigMap，并读取 Pod 终态。
- 每个候选由一次性 Job 执行。Job 不挂载 ServiceAccount Token，不持有 Secret，基线 Git mirror 只读，补丁和工作区位于临时卷，NetworkPolicy 禁止全部 ingress/egress。结果由协调器读取 Kubernetes 终态生成，候选不能自行声明通过。

两个 Worker 都使用 `python -m evolution.temporal.worker`，但分别设置 `RATSNEST_EVOLUTION_WORKER_ROLE=controller` 和 `sandbox-coordinator`。控制 queue 是 `ratsnest-evolution-production`，评测 Activity 固定投递到 `ratsnest-evolution-sandbox`；二者均单并发。占位镜像必须同时替换成同一个经过验证的 `repository@sha256:<digest>`。Git mirror PVC 必须由独立、受审计的发布流程预置准确 commit，且不得包含 deploy key、credential helper 或远端 Token。

```powershell
$context = "production-primary"
$image = "ghcr.io/ratsnestteam/ratsnest-evolution-worker@sha256:<64-hex-digest>"
kubectl --context $context apply -k deploy/k8s/overlays/evolution-worker
kubectl --context $context -n ratsnest set image `
  deployment/ratsnest-evolution-controller controller=$image
kubectl --context $context -n ratsnest-evolution-sandbox set image `
  deployment/ratsnest-evolution-sandbox-coordinator coordinator=$image
```

先确认两个 Deployment 仍为 0；再替换三类 suite digest、预置只读 mirror，并根据集群实现收紧协调器到 kube-apiserver 的精确 egress（托管集群不能直接照搬示例 selector）。随后先启动协调器，再启动控制器：

```powershell
kubectl --context $context -n ratsnest-evolution-sandbox scale `
  deployment/ratsnest-evolution-sandbox-coordinator --replicas=1
kubectl --context $context -n ratsnest scale `
  deployment/ratsnest-evolution-controller --replicas=1
```

Java 只允许平台管理员创建 Trial。Python 必须回传与部署要求相同的 `executorMode=kubernetes_job`，并绑定 candidate/base/input/suite/patch/report digest；通过后状态只能到 `awaiting_approval`。独立的批准 API 还会复核同一个 PASSED Trial、report digest 和 CAS row version。批准不会触发 merge、push、promote 或 deploy，这些仍属于外部发布流程。

V11 会在发现遗留 `PENDING` Trial 或重复 Temporal workflow 绑定时 fail-fast。升级前先停入口并查询：

```sql
SELECT trial_id, candidate_id, temporal_workflow_id, created_at
FROM control_plane.evolution_trials
WHERE verdict = 'PENDING'
ORDER BY created_at;
```

只能依据已有审计证据把确认废弃的旧 Trial 显式标记 `CANCELLED`；不得伪造 report digest 或 PASSED proof。若无法判断，应停止 V11 发布并保留旧应用读取能力。

停用时先停止创建 Trial，等待两个 queue 无 running/pending Activity，然后依次把控制器、协调器缩回 0。生产验收至少保存：镜像 digest、mirror commit、Manifest 与 suite digests、RBAC/NetworkPolicy 实测、Temporal workflow/trial ID、Job 终态、清理结果、权威 report digest、人工批准审计。仓库提供实现和静态门禁，但真实集群隔离、CNI egress、Pod Security、PVC 供应和故障演练仍必须在目标集群验收。
