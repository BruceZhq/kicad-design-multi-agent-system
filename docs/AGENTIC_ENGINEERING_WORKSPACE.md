# 工程 Agent 主动查证与候选修复闭环

本文描述工作区代码实现，不将单元测试通过等同于真实板卡 `release_ready`。

## 目标与不变边界

保留 LangGraph 多角色编排、Temporal 耐久执行、17 个发布阶段、真实 KiCad 检查和检查点。改变的是阶段内部的工作方式：模型可以先提出工具查询，收到真实工程观察后继续规划，再通过现有 CAD 候选事务执行修改。

发布权始终属于确定性检查。模型没有删除 ERC/DRC、修改用户约束、伪造器件证据、读取密钥或直接写生产 Harness 代码的权限。

## 运行链路

```text
PipelineStep.run
  → 绑定当前任务的 EngineeringWorkspace 与阶段 Skill
  → propose_structured
      → LLM 返回 engineering_queries（可选）
      → 读取当前任务文件 / 方案 / 库 / 几何 / 生成器源码
      → 查询结果返回同一有界模型会话
      → LLM 返回原有结构化方案或 RecoveryDecision
  → 源 IR / PCB 候选动作
  → 原有阶段检查
  → 保留通过或实质改善的候选 / 回滚 / 更新故障假设
```

不要求模型供应商支持原生 function calling 或 `response_format`。JSON 查询协议复用现有 LLM 客户端，因此查询前后的调用仍进入现有 token 统计、超时和预算控制。没有需要查询的信息时，模型可以在首次调用直接返回最终方案，不增加额外调用。

## 只读工具

实现在 `src/ratsnestpro/orchestration/engineering_workspace.py`。

| 工具 | 输入重点 | 返回内容 |
| --- | --- | --- |
| `files` | `offset`, `limit` | 当前 run 目录中允许读取的工程文件列表 |
| `read_file` | `path`, `offset`, `limit` | 工程文件的带行号文本；偏移量从 0 开始 |
| `artifact` | `step`, `pointer`, `offset`, `limit` | 当前结构化方案；支持 JSON Pointer 和分页 |
| `symbol` | `lib_id` | 安装库中的真实引脚和符号属性 |
| `footprint` | `lib_id` | 安装库中的真实局部焊盘坐标、层和 courtyard 边界 |
| `pcb` | `section`, `reference`, `net` | 当前 PCB 的元件、绝对焊盘坐标、走线、网络、铜区或板信息，以及文件哈希 |
| `source` | 源码别名、行偏移和数量 | 指定生成器/校验器的源码窗口，只读 |
| `render` | 实体 CAD 路径、可选铜层/丝印层 | KiCad CLI 渲染的 PNG，附源文件哈希；图片内容进入模型消息 |

例如：

```json
{
  "engineering_queries": [
    {"tool": "artifact", "step": "selection", "pointer": "/parts", "offset": 100, "limit": 10},
    {"tool": "pcb", "section": "pads", "reference": "U1"}
  ]
}
```

结果被标记为不可信输入证据，不能作为系统指令或发布许可。每个结构化提案尝试最多允许 6 个查询，每批最多 3 个，单项观察最多 16,000 字符。超大观察返回明确错误，要求缩小范围，不截断 JSON 冒充完整结果。相同会话的重复查询不会重新执行；查询依然消耗额度，防止空转。

文件路径解析后必须仍位于当前 run 目录，限制文件类型并排除密钥、凭据、模型日志等文件名。源码只允许命名白名单，不能传任意主机路径；库查询只接受 KiCad lib_id。此接口没有 shell、写文件、联网或安装依赖的执行通道。跨任务授权继续由创建 PipelineContext 的服务边界负责。

## 修复执行

### 布局阶段不再只有固定 packing 重试

`layout_critical` 和 `layout_general` 在 PCB 尚未写出时也支持已有的 `CadActionBatch`：移动、旋转和交换位置在深拷贝的 placement IR 上原子执行。动作必须绑定当前 IR 指纹并通过前置条件，不能改板框、器件身份或层面。批次中任何一个动作失败，整个批次不提交。

同时，模型可以返回最多 32 个器件的 `PlacementPatch`。原有布局算法仍负责快速产生种子候选；反思提出不同策略后，模型的坐标修改会真正进入候选，随后运行原有约束检查，并由 `layout_write` 写成 PCB。IR 改动本身不等同于已经落盘，更不等同于 DRC 通过。

已生成 PCB 的修改继续复用原有 fingerprint-bound CAD worker、真实文件修改、独立检查和候选回滚路径。

### 引脚证据指导归因，不关闭反思

结构化 ERC 证据仍提供首选责任步骤，但不再因为有一个实体 owner 就跳过 LLM。模型可以先查当前 IR 和源码，再确认或反证归因。上游目标仍受已有步骤、执行预算和不可变需求约束，不能跳过检查或回滚修改原始用户要求。

网表补丁如果会被所有权范围部分裁剪，会在执行前明确拒绝，并将允许范围返回模型修正。不会把一个被悄悄裁掉引脚的补丁当作完整执行成功。

### 验证与跨步骤候选

默认进展分数依据结构化违规数量，而非错误消息字符长度。布线和制造阶段保留各自的物理指标评分。

上游重规划的候选在中间设计门禁失败后，默认可再进行最多 3 次有界局部修复；计数保存在 `ReplanRecord.intermediate_repair_attempts`，恢复检查点不会清零。候选内不再开启嵌套上游重规划，基础设施/执行阻塞不通过此路径延长。只有所有受影响阶段通过，才提交整个上游候选；预算用尽或无可执行动作时恢复原基线。

## 器件分类修复

`component_preparation.py` 不再按 `sensor`、`ldo`、`mcu`、`ic` 的任意子串把器件判为有源器件。分类同时考虑真实通用符号、参考标号及角色中的器件/支持功能词。

因此 `Device:C / C5 / motion_sensor_vdd_decoupling` 按被动件处理，而不是要求其提供传感器芯片身份。真实 IC 使用通用电阻符号冒充的检测仍保留；采购身份不确定也不自动变为采购已验证。

## Reviewer 自动回访

独立 Reviewer 的确定性结果若包含可执行的工程缺陷，会生成 `review_repair.json`：绑定原检查点、实际 CAD 和 ERC/DRC 报告 SHA-256，保留引脚、UUID、位置证据，确定需要失效的最早责任阶段。它不是发布许可。

LangGraph 从 Reviewer 返回 Hardware Engineer，建立新的 Temporal continuation ID，但沿用同一工作区和原始需求。加载器只允许与证据收据一致的后缀失效，保留前缀，不把旧的已通过候选再检查一遍冒充修复。`review_feedback` 进入模型主动观察会话；生成完实体后重新走下游检查和独立 Reviewer。

默认最多自动回访两次，计数跨恢复保存；相同故障证据再次出现即停止。KiCad 不可用、检查未实际执行等基础设施问题不自动归给原理图设计。未知所有权继续保持明确不可执行状态，不盲目从第一步重来。

## 图面、分网络布线与视觉反馈

- `SchLayoutPlan.label_nets` 已进入 `materialize_pinmapped`。非标签网络尝试实际正交局部导线，避开其它网络的引脚、导线和符号包围区域。无安全路径则保留电气有效的标签连接，并在 `schematic_drawing.json` 记录回退；不会画一条跨网短路的线来美化图面。没有真实引脚坐标直接报错，不再生成悬空的网格标签。
- `NetClass.nets` 表达精确网络成员。旧 power/signal/default 计划根据现有供电网列表兼容绑定；重复、未知或无归属网络会被拒绝。规则写入 `.kicad_pro` 和真实 DSN；关键网络先单独路由、导入实体，再导出完整板继续路由。两个阶段共享超时预算，旧 SES 不可用于证明新执行成功。
- 路由后不仅运行 DRC，还独立读取实际走线与过孔，检查各网络线宽、孔径。KiCad 的建议线宽不等同于 DRC 的最小线宽，不能仅检查 JSON 计划。`routing-rules-receipt.json` 保存执行的网络分类和路由阶段。
- `render` 调用真实 `kicad-cli` 导出 SVG，再用 `rsvg-convert` 转 PNG。渲染以 CAD 哈希缓存，每个观察会话最多两图；几何修复时自动提供已有实体的图像。图片以多模态消息传给同一模型，复用预算核算。模型接口不支持图像时明确报告“未查看图片”，退回几何观察，不伪造视觉结论。多页原理图目前展示第一页，同时报告总页数。

服务镜像包含 `librsvg2-bin`；已有镜像需要重建才有新的渲染依赖。无需新增图像生成服务。

接口实现依据 [KiCad 9 CLI 文档](https://docs.kicad.org/9.0/en/cli/cli.html)；发布判断始终来自本项目的实际文件和检查结果，而非这些接口文档本身。

## 生成器补丁隔离验证与治理晋级

复用已有 Evolution Candidate/Trial，不在任务进程直接改生产代码。

1. 策略增加独立的 `allowedGeneratorGlobs`，只允许四个生成器文件：`materialize.py`、`schematic_wiring.py`、`routing_rules.py`、`_route_worker.py`。发布门禁、检查点收据、身份边界、评测器和密钥路径仍不可修改。
2. 补丁基于固定 commit 和原文件哈希，在隔离 checkout 中落盘。生成器补丁的 `python-compile` 槽位升级为固定的真实 CAD probe，同时保留既有优化集、holdout、adversarial 检查。
3. `generator_validation.py` 的可信父进程不导入候选代码：候选子进程生成真实原理图、PCB、DSN/SES；父进程用独立 KiCad CLI 和 KiCad Python 复验 ERC、DRC、引脚连接、层数和分网络线宽。生产验证器来自只读 evaluator 镜像，不接受候选提交的成绩单。`generatorCadValidated` 随签名结果绑定进 Trial；本地开发 sandbox 不能批准生成器生产晋级。
4. 通过后仍需平台管理员批准。CI/管理员另行构建、注册干净的 HarnessVersion：`parentVersionId` 指向被评测基线，`bundleDigest` 绑定 Trial 的补丁哈希，source commit、tree digest、image digest、manifest 均必须匹配。Trial 检查不会替你偷偷合并 Git 或部署容器。
5. `:canary` 现在真正调用既有 `HarnessVersionService.configureCanary`。默认 10% 灰度；候选、Trial 绑定及 rollout 更新同一事务提交，有任意 CAS 冲突则整体回滚。构建来源 digest 为注册版本的 `sourceCommit/sourceTreeDigest/patchSha256/imageDigest/manifestDigest` 五字段按键排序紧凑 JSON 的 SHA-256。
6. `GET /api/v1/platform/evolution/candidates/{id}/canary-report?trialId=...` 返回服务端聚合证据。`POST /api/v1/platform/evolution/candidates/{id}:promote-verified` 接受 `expectedVersion/trialId/reportDigest/reason`，不接受手填成功率。默认要求至少五个独立根任务全部完成严格发布，且可信产物清单中存在原理图、PCB、DSN、SES、ERC/DRC 报告。失败、未完成和缺清单的任务都纳入；重复续跑不能增加独立任务数。统计口径是服务端已验证的发布清单，不是重新跑一遍 KiCad 或新增性能对比评测。
7. 晋级调用真实 rollout 的 `promote`，保留旧稳定版本作为回滚目标；聚合报告及摘要保存在 Trial guardrails，原始评测报告不会被覆盖。回滚复用 Harness 管理 API。

### 部署接线

在 Java 配置 `RATSNEST_AGENT_RUNTIME_VERSION_ENDPOINTS`，为新旧版本分别部署 Runtime/API 和匹配的 EDA Worker。例如（仅内部地址示例）：

```json
{"harness-v1":{"http":"http://runtime-v1:8080","grpc":"runtime-v1:9090","plaintext":true},"harness-v2":{"http":"http://runtime-v2:8080","grpc":"runtime-v2:9090","plaintext":true}}
```

HTTP/gRPC、SSE 和恢复请求按**固定版本**路由，不能因 channel 从 canary 变 stable 就切回旧镜像。不同版本使用不同 `RATSNESTPRO_TEMPORAL_TASK_QUEUE`；同版本 API 与 Worker 使用同一队列，并配置匹配的 `RATSNEST_HARNESS_VERSION_ID` 和 `RATSNEST_HARNESS_MANIFEST_DIGEST`。Worker 会拒绝错版本任务。晋级不会迁移正在运行的会话；旧服务应保留到旧任务完成或有显式兼容迁移。

配置 `RATSNEST_EVOLUTION_CANARY_PERCENT`（1–25）和 `RATSNEST_EVOLUTION_CANARY_MINIMUM_SAMPLES`（至少 3，默认 5）。初次灰度建议使用专门的试点租户和硬件任务集；不要把这一严格发布口径当作普通聊天任务的成功标准。未注册可信镜像、未配版本路由或没有实际灰度数据时，晋级应拒绝。

## 验证口径

定向测试覆盖主动查询后输出方案、文件越界拒绝、超过旧快照前缀的分页、重复查询上限、真实 PCB 文本焊盘坐标读取、布局动作原子性、模型布局补丁、结构化评分、网表范围拒绝，以及上游候选的中间修复与提交。

这些测试不调用真实 LLM、不启动 Docker，也不证明板卡 ERC/DRC 或量产可用性。部署后还需要以冻结需求、相同模型与预算运行黄金板型，对比最终 ERC、DRC、未连接数、路由产物及 `release_ready`，并独立抽查图面质量。

新增定向覆盖：Reviewer 哈希绑定与无进展停止、图面避障、分网络规则落盘、PNG 内容进入模型、生成器路径治理，以及 Java 真实 rollout 调用和灰度样本去重。真实 CAD probe 是部署后的执行检查，不把 mock 单元测试冒充其实际通过结果。生产隔离仍依赖既有 Kubernetes 只读根文件系统、无凭据、限额与网络策略；不能把本地进程隔离宣称为恶意代码安全沙箱。

2026-09-05 本地验证：Python 定向组合 67 项通过，Ruff 与 diff whitespace 检查通过。Java 灰度/路由测试已补齐，但本机仅有 JDK 17、Docker daemon 未运行，本轮未编译 Java 21 后端，也未运行真实模型/CAD E2E 或生产晋级。上述部署接口需在构建新镜像后验收，不能把源码接通直接当成发布成功率提升的实测结果。
