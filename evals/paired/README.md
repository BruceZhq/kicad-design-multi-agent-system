# 同题单 Agent / 多 Agent 配对评测

这套资产比较的是编排方式，而不是“强模型对弱模型”。每个 `pairId` 的两臂使用完全相同的自然语言需求、模型、能力档案、超时、工程预算、KiCad 资产快照、证据工具类别、17 步 Temporal EDA 流水线、确定性 Reviewer 和发布门禁。唯一预期自变量是：

- `single_agent`：一个 LangGraph 节点、一个连续上下文、没有角色 handoff；
- `multi_agent`：生产 `Supervisor → Architect → Parts → Hardware → Reviewer` 编排。

单 Agent 不是刻意削弱的 baseline。它拥有与生产 Architect/Parts 相同类别的本地库、内部知识、KiCad 官方文档、官方厂商搜索、通用 Web 搜索、Datasheet 获取、器件目录和显式 symbol/footprint binding 校验。两个 arm 最终都走同一套确定性工程执行与审查。

## 资产

- 黄金计划：`frontend/public/evals/paired-kicad-golden.v1.json`
- 冻结配置：`evals/paired/frozen-config.v1.json`
- KiCad 资产快照：`evals/paired/kicad-assets.v1.json`
- 输出 schema：`evals/paired/paired-result.v1.schema.json`
- 盲审量表和空清单：`evals/paired/blind-review-rubric.v1.json`、`evals/paired/blind-review-template.v1.json`
- 浏览器手工记录模板：`evals/paired/manual-run-record-template.v1.json`

黄金集包含 10 个完整 pair、20 次真实执行，覆盖 foundation、subsystem、controller、integrated 四层板型。题目没有“请触发 HITL”“调用某角色”等评测暗示；器件绑定来自运行时已安装 KiCad 库的只读预检。`symbolLibrarySha256` 是整个 `.kicad_sym` 库文件摘要；`footprintFileSha256` 是单个 `.kicad_mod` 文件摘要。

## 部署后冻结

代码重建会改变容器 image ID，所以仓库中的 `preflightBaseImageId` 只是资产设计时的预检基准，不能冒充最终部署身份。正式测量前，在最终 Agent 镜像中执行只读验证：

```powershell
docker run --rm --read-only --entrypoint python `
  -v "${PWD}:/workspace:ro" `
  <FINAL_AGENT_IMAGE> `
  /workspace/scripts/verify_paired_kicad_assets.py `
  --manifest /workspace/evals/paired/kicad-assets.v1.json
```

记录 `docker image inspect <FINAL_AGENT_IMAGE> --format '{{.Id}}'` 和该镜像中 `kicad-cli --version` 的真实输出。然后显式冻结快照；这是唯一会修改评测资产的步骤：

```powershell
docker run --rm --entrypoint python `
  -v "${PWD}:/workspace" `
  <FINAL_AGENT_IMAGE> `
  /workspace/scripts/verify_paired_kicad_assets.py `
  --manifest /workspace/evals/paired/kicad-assets.v1.json `
  --config /workspace/evals/paired/frozen-config.v1.json `
  --plan /workspace/frontend/public/evals/paired-kicad-golden.v1.json `
  --blind-template /workspace/evals/paired/blind-review-template.v1.json `
  --freeze `
  --runtime-image-id '<REAL_SHA256_IMAGE_ID>' `
  --kicad-version '<REAL_KICAD_VERSION>'
```

脚本会重新解析每个 symbol/footprint、核对 pin/pad 集合和文件摘要，写入真实 UTC `checkedAt`，并按资产 → 配置 → 计划 → 盲审清单的顺序重算摘要。任何不存在、不兼容或漂移的资产都会 fail closed。

## 顺序运行

评测必须在相同资源条件下串行执行，不能让两臂并发争抢 LLM、Temporal worker、CPU 或路由器。计划已经 counterbalance：奇数 pair 先 single，偶数 pair 先 multi。按 JSON 中的 case 顺序运行即可；中断时保留报告，恢复后从尚未记录的 case 继续。

评测专用 runtime 使用 `langgraph.eval.json` 并显式开启 `RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED=true`。生产 `langgraph.json` 仍只注册 `ratsnestpro-multi-agent`。两个 arm 都必须经相同的受认证 Agent Runtime 入口；服务端 token 只通过 `AUTH_SECRET` 环境变量给 runner，绝不能放入 `NEXT_PUBLIC_*` 或浏览器。

```powershell
$assetDigest = (Get-FileHash evals/paired/kicad-assets.v1.json -Algorithm SHA256).Hash.ToLower()
$configDigest = (Get-FileHash evals/paired/frozen-config.v1.json -Algorithm SHA256).Hash.ToLower()

python -m evolution.live_runner `
  --root . `
  --plan frontend/public/evals/paired-kicad-golden.v1.json `
  --endpoint http://127.0.0.1:8080/ratsnestpro-multi-agent/stream `
  --single-agent-endpoint http://127.0.0.1:8080/ratsnestpro-single-agent-eval/stream `
  --model deepseek-v4-flash `
  --provider deepseek `
  --environment-digest $assetDigest `
  --config-digest $configDigest `
  --output evals/reports/paired-kicad-golden.json
```

可以用重复的 `--case golden.p01.single --case golden.p01.multi` 只运行一个完整 pair。不要只挑一个 arm 后计算 pair delta。

模型供应商当前不保证 temperature/seed 可完全固定，因此冻结配置如实记录为 `null`，不宣称推理逐 token 确定性。报告还记录 source commit/dirty、plan digest、每例 frozen input digest 和真实时长，以便识别漂移。

## 前端入口

生产默认关闭。受控评测部署需同时设置
`RATSNEST_EVAL_MODE_ENABLED=true` 与
`RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED=true`，然后打开 `/evaluation`。该页可在同一
`pairId` 下切换两臂、核对冻结 prompt/config、导出记录，或点击“在工作区运行此臂”。

工作区执行不建立浏览器到 Python 的旁路：它继续经过 OIDC、Next BFF、Java 控制面、
持久化、SSE 和原有 artifact 链路。BFF 会从部署内计划重新校验 plan/prompt SHA-256、
case、arm、模型和能力范围；Java 只接受两个固定 Agent ID，且单 Agent 开关默认关闭。
每个评测 case 强制创建独立根会话，运行配置中持久化 `evaluation_context`，便于之后按
`pairId + arm + runId` 回收真实数据。批量无人值守执行仍使用上述 `live_runner`。

## 盲审与指标口径

先完成所有执行，再把 arm、agentId、角色名和叙述性模型文本隐藏，随机分配中性 case ID 给审查员。审查员只看用户需求、实际 KiCad/制造文件、ERC/DRC/布线事实和发布结论，并按六维量表填写外部 review manifest。缺少人工标签时 `humanAcceptance=null`，不能当成功。

报告同时保留全体 `armMetrics` 和只基于完整 pair 的 `pairedComparison`：

- `transportCompletionRate`：HTTP 请求完成；
- `protocolCompletionRate`：SSE 终止契约完成；
- `pipeline17StepCompletionRate`：17 步全部完成；
- `strictTaskSuccessRate`：所有预声明检查均未失败，简历里的“端到端任务完成率”默认使用此口径；
- `releaseReadyRate`：确定性发布门禁真正通过；
- `humanAcceptanceRate`：有盲审标签样本中的人工接受率；
- 阶段/工具契约错误率、平均/中位/P95 时长、HITL 介入率与请求数、工具参数 schema/后置条件正确率；
- handoff 只对多 Agent 报告，单 Agent 固定为 `not_applicable`，不是 0 次成功。

P95 仅在 arm 样本数不少于 5 时报告。`metricDeltas` 为 `multi_agent - single_agent`，只使用两臂都已完成的 pair，并明确输出 `deltaDenominatorCompletePairs`；中途报告或缺臂样本不会混入配对差值。任何缺少的事件证据都是 `null`/N/A，绝不补成成功。
