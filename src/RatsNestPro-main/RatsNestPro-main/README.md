# RatsNestPro

一个**独立的、EricAI 驱动的 PCB 设计与审查 Agent**。

与"一个 MCP 插件"不同,RatsNestPro 拥有自己的 **Agent 大脑**、类型化的**领域模型**、
确定性的**验证/兜底层**,以及**两条显式的编排流程**。MCP 只是一个*可选*的对外接口
(未来工作),不是它的架构。

它的核心分工始终是:

> **LLM(EricAI)只负责"读懂 / 判断 / 解释 / 提议 / 命名 / 选型接地";
> 确定性代码负责"计算 / 验证 / 裁定 / 执行"。**

项目提供两条互补的编排流程:

1. **族内闭环生成(管线 A)** —— 只认证**一个电路族**(ATmega328P USB-C 开发板)。
   参数化生成一张可在 KiCad 10 打开的原理图,跑确定性 gate + ERC,失败可进修复循环。
   这是一条完整闭环、经过验证的"纵向切片"。
2. **知识驱动 PCB 全流程管线(管线 B)** —— 一条**固定 17 步**的行业标准流程
   (需求→拓扑→选型→原理图→布局→布线→制造),每步统一形态:
   **注入该步知识 → LLM 结构化产出 → 廉价"防烧板"兜底校验**。它把范围推进到
   **PCB 布局与布线**,并以真实符号/封装库和工艺表为硬事实来源。

两条流程共用同一套权力边界、领域合同、EDA 适配层、验证理念与知识库。

---

## 目录

- [1. 一句话定位](#1-一句话定位)
- [2. 核心设计原则](#2-核心设计原则)
- [3. 总体架构](#3-总体架构)
- [4. 目录结构](#4-目录结构)
- [5. 安装(离线核心)](#5-安装离线核心)
- [6. EricAI 配置](#6-ericai-配置)
- [7. KiCad 库与工艺表配置](#7-kicad-库与工艺表配置)
- [8. 命令行用法](#8-命令行用法)
- [9. 管线 A:族内生成 + 验证 + 修复](#9-管线-a族内生成--验证--修复)
- [10. 管线 B:知识驱动的 17 步 PCB 全流程](#10-管线-b知识驱动的-17-步-pcb-全流程)
- [11. 三个 Agent 角色](#11-三个-agent-角色)
- [12. 两层知识:硬事实与软知识](#12-两层知识硬事实与软知识)
- [13. EDA 适配层与真实库](#13-eda-适配层与真实库)
- [14. 审查任意 KiCad 工程](#14-审查任意-kicad-工程)
- [15. 接地选型](#15-接地选型)
- [16. 产物与运行目录](#16-产物与运行目录)
- [17. 测试与质量门禁](#17-测试与质量门禁)
- [18. 实现状态与能力边界](#18-实现状态与能力边界)
- [19. 溯源与许可证](#19-溯源与许可证)

---

## 1. 一句话定位

RatsNestPro 接收自然语言需求,提供三种能力:

- **设计(生成):** 两条流程可选 —— 管线 A 在 ATmega328 族内生成并验证**原理图**;
  管线 B 走固定 17 步,把设计推进到**布局与布线**,产出 `.kicad_sch` + `.kicad_pcb` +
  BOM/CPL(+ 有 kicad-cli 时的 Gerber)。
- **审查:** 对任意已有 KiCad 工程做确定性分析并产出结构化 Markdown 评审报告。
- **修复:** 管线 A 的 gate 失败时,在类型化白名单内提议修正、执行、重验(半自动或全自动)。

族内**参数化**意味着:不同需求 → 不同参数 → **不同的板子**(晶振、LDO、去耦数量、
电源 LED、排针、安装孔、GPIO 映射都可能不同),但每一处差异都在白名单内、且被
确定性 gate 验证——既有差异,又不会生成电气错误的电路。

---

## 2. 核心设计原则

不可违背的权力边界:

```
LLM 提议、判断、解释
    ↓
Pydantic 合同校验结构(非法即 fail closed)
    ↓
类型化受控工具执行副作用(LLM 不能任意写文件 / 跑 shell)
    ↓
Circuit IR / 步骤合同 表达批准的设计意图
    ↓
KiCad 产物表达实际生成结果
    ↓
确定性验证器 + ERC + 工艺兜底 决定 Gate(权威)
    ↓
(人工工程师决定发布 —— 属发布流程)
```

- 所有 LLM 输出经 Pydantic 结构化校验(`extra="forbid"`),非法即 fail closed。
- **硬事实**(元件目录、真实引脚号、焊盘几何、gate 阈值、负载电容公式、工艺最小值、
  跨参数规则)由确定性代码查真实库/表,是**权威**,**不进模糊向量库**。
- **软知识**(设计模式、最佳实践、错误分类学、layout/routing 经验)进检索知识库,
  是**顾问**,其影响仍走下游校验/兜底。
- 精确数值由确定性公式计算或查真实表;LLM 至多说"用哪个公式 / 什么策略"。
- 整个确定性核心**完全离线可安装、可测试**(EricAI 是惰性可选导入)。

---

## 3. 总体架构

```text
自然语言需求
  → Agent 层:   Architect(族判断+参数) / Coder(修复提议) / Reviewer(解释+分诊)   [EricAI]
  → 领域层:     CircuitIR / BoardPlan / Finding / Gate / Atmega328Params            [Pydantic]
                + 17 步管线合同(TopologyPlan / SelectionPlan / NetlistIntent / ...)
  → 编排层:     管线 A(plan→物化→ERC→验证→修复→审查)
                管线 B(17 步:需求→拓扑→选型→原理图→布局→布线→制造)
  → EDA 适配层: vendored kicad-mcp-py 核心(进程内、类型化,不走 MCP)
                + 真实符号库(symbols.py)/ 真实封装库(footprints.py)
  → 验证/兜底层:确定性 IR 规则 + kicad-cli ERC + 工艺"防烧板"兜底 → Gate / CheckResult
  → KiCad 文件:.kicad_sch / .kicad_pcb(+ BOM / CPL / Gerber)

  检索知识库(EricAI embedding+reranker / 离线词法回退)按 role 横向服务各步与各角色
```

---

## 4. 目录结构

```text
RatsNestPro/
├── pyproject.toml                    # 打包、依赖、ruff/mypy/pytest 配置
├── README.md
├── .env.example                      # KiCad 库路径 / 工艺表 环境变量样例
├── src/ratsnestpro/
│   ├── cli.py                        # CLI:design-plan / design / review / parts / pcb
│   ├── config.py                     # .env 加载 + KiCad 库路径解析 + ProcessCapability 工艺表
│   ├── __main__.py
│   ├── data/
│   │   └── process_capability.json   # 工艺最小值(默认 JLCPCB 常规 2 层)—— 硬事实
│   ├── domain/
│   │   └── contracts.py              # Pydantic 合同(ContractModel/IR/BoardPlan/Finding/Gate/DesignPlan/...)
│   ├── families/
│   │   └── atmega328.py              # Atmega328Params + 参数化 build_ir/build_plan/expectations_for
│   ├── verification/
│   │   ├── expectations.py           # Expectations(由参数派生的硬事实期望)
│   │   ├── rules.py                  # 确定性规则(CAT/REF/VLT/DEC/XTL/LDO/GPIO/HDR)
│   │   └── verify.py                 # verify_design():规则 + ERC → VerificationReport
│   ├── eda/
│   │   ├── adapter.py                # SchematicDoc 类型化门面 + run_erc
│   │   ├── materialize.py            # IR + BoardPlan → 真实 .kicad_sch
│   │   ├── symbols.py                # 真实符号库:lib_id→引脚(号/名/坐标),KiCad10 extends 继承
│   │   ├── footprints.py             # 真实封装库:lib_id→焊盘/包围盒(重叠/越界兜底用)
│   │   └── vendor/                   # vendored kicad-mcp-py 核心(勿改;见 §19)
│   ├── agents/
│   │   ├── llm.py                    # LlmMode + LLMClient 协议 + EricAIClient(惰性)+ resolve_client
│   │   ├── heuristics.py             # 离线关键词参数提取 + 族判断(params_from_requirement)
│   │   ├── architect.py              # Architect.plan()(族判断+参数+澄清)
│   │   ├── reviewer.py               # Reviewer.review()(叙述+分诊+Markdown)
│   │   └── coding.py                 # Coder.diagnose()(根因诊断+白名单修复)
│   ├── orchestration/
│   │   ├── generate.py               # 【管线 A】generate_design() 单向生成闭环
│   │   ├── repair.py                 # 【管线 A】run_repair() 半自动/自动修复循环
│   │   ├── review_project.py         # review_project() 独立审查
│   │   ├── pipeline.py               # 【管线 B】17 步 Pipeline + 步骤基类 + 运行器
│   │   └── pipeline_contracts.py     # 【管线 B】每步 Pydantic 输入输出合同
│   ├── knowledge/
│   │   ├── store.py                  # KnowledgeBase(按 role 的向量/词法检索)+ build_default_kb
│   │   └── corpus/*.md               # 16 篇软知识语料(按 role 标签组织)
│   └── parts/
│       └── selector.py               # 接地选型(本地 JLCPCB SQLite 缓存)
└── tests/                            # 26 个测试文件,143 个测试(含可选 real_kicad)
```

---

## 5. 安装(离线核心)

确定性核心**完全离线**安装与测试,不需要 EricAI、不需要联网。Python 3.12–3.14。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

若你有 `uv`:

```powershell
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest -q
```

真实 EDA 路径需要本地安装 **KiCad 10**(`kicad-cli` 会被自动发现);缺失时 ERC / Gerber
报告为 `unavailable`,**绝不被当作通过**。

---

## 6. EricAI 配置

EricAI 同时驱动三个 Agent 与 17 步管线的每步决策(`openai/gpt-oss-120b`),以及知识库检索
(embedding `BAAI/bge-m3`、reranker `BAAI/bge-reranker-v2-m3`)。它采用 **SSO 设备码认证,
无需 API key**。

前置条件:

1. 连接 **Ericsson 内网 / VPN**。
2. 从内网索引安装:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install --index https://arm.sero.gic.ericsson.se/artifactory/api/pypi/proj-swtech-pypi-local/simple ericai
   ```
3. 登录一次:`ericai --ericsson-test-connectivity`
4. **每个新终端**都要设代理绕过:
   ```powershell
   $env:NO_PROXY=".gic.ericsson.se,.sero.gic.ericsson.se,localhost,127.0.0.1"
   ```

`--llm` 选择模式(等价环境变量 `RATSNESTPRO_LLM`):

| 模式 | 行为 |
|---|---|
| `offline`(默认) | 走确定性路径 / 每步用确定性兜底,**不发任何模型请求** |
| `auto` | EricAI 可达时使用;任一步提议失败**回退确定性** |
| `required` | 必须使用 EricAI;缺库/不可达/输出非法则 **fail closed(报错,不静默回退)** |

别名:`off`/`disabled`/`none`→`offline`;`live`/`require`/`required`/`ericai`→`required`。

> 说明:`ericai` 仅在 Ericsson 内网可安装。项目以**惰性导入**接入它,离线核心不受影响;
> 单元测试通过注入 fake client 覆盖 LLM 路径,无需联网。

---

## 7. KiCad 库与工艺表配置

管线 B 的硬事实来自真实库与工艺表(不靠 LLM 记忆)。复制 `.env.example` 为 `.env`:

```
KICAD_SYMBOL_DIR=...\kicad-symbols-master
KICAD_FOOTPRINT_DIR=...\kicad-footprints-master
# 可选:覆盖默认工艺表
RATSNESTPRO_PROCESS_CAPABILITY=...\my_fab.json
```

- `config.py` 在导入时**加载一次 `.env`**(已存在的环境变量优先),然后 `symbol_dir()` /
  `footprint_dir()` 从上述变量解析出第一个存在的目录。
- **工艺能力表**(`ProcessCapability`)提供最小线宽 / 间距 / 过孔外径 / 过孔钻孔 / 环宽 /
  孔径 / 板边间距 / 丝印宽度 / 层数选项等**权威下限**。解析顺序:
  显式路径 → `RATSNESTPRO_PROCESS_CAPABILITY` → 内置 `data/process_capability.json`
  (保守的 JLCPCB 常规 2 层)。
- 库未配置时,相关兜底以 **WARNING(unavailable)** 呈现——**绝不当作通过,也不阻断流程**。

---

## 8. 命令行用法

```powershell
# 【管线 A】1) 生成不可变计划(审批边界的 JSON)
ratsnestpro design-plan "ATmega328 开发板,USB-C,3.3V LDO,16MHz,电源LED" --out runs/demo

# 【管线 A】2) 生成并验证原理图(加 --llm auto|required 使用 EricAI)
ratsnestpro design "ATmega328 开发板,USB-C,8MHz,不要LED" --out runs/demo

# 【管线 A】3) 生成;若被阻断则跑修复循环(默认半自动;--auto 全自动)
ratsnestpro design "ATmega328 16MHz 5V" --out runs/demo --repair --max-iter 5

# 【管线 B】4) 跑完整 17 步知识驱动 PCB 管线
ratsnestpro pcb "ATmega328 USB-C 3.3V 8MHz 开发板" --out runs/pcb --project board --llm offline

# 5) 审查任意已有 KiCad 工程(独立于生成)
ratsnestpro review path/to/project --out review.md

# 6) 基于本地 JLCPCB 缓存做接地选型
ratsnestpro parts "10k 0603"
```

也可用 `python -m ratsnestpro ...` 调用。

**管线 A 的显式参数标志**(总是覆盖 Architect / 关键词提取的选择):
`--crystal {8,16}`、`--ldo {3.3,5.0}`、`--decoupling N`、`--led/--no-led`、
`--rows {1,2}`、`--pins N`、`--holes {0,4}`。

**`design` / `design-plan` 退出码**:`0` 成功;`1` 被阻断(gate 失败);
`2` LLM 必需但不可用 / 参数矛盾;`3` 不属于认证族或需要澄清(会打印澄清问题)。

**`pcb` 退出码**:`0` 全部步骤通过;`1` 在某步被兜底阻断(会逐步打印 OK/BLK 与失败项);
`2` LLM 必需但不可用。

---

## 9. 管线 A:族内生成 + 验证 + 修复

电路族定义在 `families/atmega328.py`。参数合同 `Atmega328Params` 带白名单与跨参数规则:

| 参数 | 取值域 | 说明 |
|---|---|---|
| `crystal_mhz` | 8 / 16 | 晶振频率;联动负载电容(8→22pF,16→18pF) |
| `ldo_output_v` | 3.3 / 5.0 | LDO 输出;联动 LDO 型号 |
| `decoupling_count` | 4–8 | 100nF 去耦电容数量 |
| `power_led` | bool | 是否加电源指示 LED(+限流电阻) |
| `breakout_rows` | 1 / 2 | breakout 排针行数 |
| `breakout_pins_per_row` | 4–12 | 每行 pin 数(pin1=电源,末 pin=GND,中间为 GPIO) |
| `mounting_holes` | 0 / 4 | M3 安装孔 |

**硬跨参数规则(合同层直接拒绝非法组合):**

- ATmega328P 速度等级:16 MHz 需要 ≥4.5 V 供电 → **16 MHz + 3.3 V 被拒绝**。
- breakout 信号 pin 总数不得超过可用 GPIO 数(12)。

默认参数复现"黄金参考板":16 MHz / 5 V、6 个去耦、带电源 LED、两排 8-pin 排针、4 个安装孔。

**生成闭环(`orchestration/generate.py`)**:`build_ir(params)` → `materialize_design` 写出
真实 `.kicad_sch` → 写最小 `.kicad_pro` → `verify_design(ir, expectations, sch_path)`
汇总 gate → 落盘 `plan.json` / `gate_report.json`。

**验证 gate 清单(`verification/`)**:任一 required 且 FAILED/ERROR 的 gate 或任一 error 级
finding → **blocked**。`UNAVAILABLE`(如无 kicad-cli 的 ERC)**既不是通过也不是阻断**。

| Gate | 检查内容 | 规则 ID |
|---|---|---|
| `plan_contract` | IR 结构合法(构造即校验) | — |
| `catalog` | 每个元件都有 catalog_id | CAT-001 |
| `reference_connectivity` | 元件有连接、无单脚信号网 | REF-001/002 |
| `voltage` | 供电网电压声明与目标一致、GND 存在 | VLT-001/002/003 |
| `six_decoupling` | 去耦数量/容值正确、连接在 rail↔GND 之间 | DEC-001/002/003 |
| `crystal_load` | 两个负载电容、容值匹配频率、回 GND | XTL-001/002/003 |
| `ldo_caps` | LDO 输入/输出电容存在且连接正确 | LDO-001..004 |
| `gpio_mapping` | breakout 信号网数量、每网连 MCU↔排针 | GPIO-001/002 |
| `headers` | 排针 pin1=供电、含 GND pin | HDR-001/002 |
| `kicad_erc` | kicad-cli ERC(有 KiCad 才跑) | ERC-* |

**修复循环(`orchestration/repair.py`)**:`run_repair(params, target, max_iter, mode, on_step)`:

1. `build_ir(params)` → `verify_design`;不被阻断 → 成功返回;
2. 被阻断 → `Coder.diagnose` 在白名单内提议修复(唯一允许 `set_param`)→ 重新校验并应用 → 重验;
3. **fail closed 条件**:Coder 放弃、无进展(参数未变 / 同一策略重复 2 次)、超出 `--max-iter`。

半自动 vs 全自动由 `on_step` 回调表达:CLI 半自动模式每步 `input()` 询问是否应用,`--auto` 跳过。
修复成功后自动重新生成板子。

> 说明:族内 happy-path 下 Architect 通常直接产出已通过的参数,修复循环为 no-op;
> 修复机制通过注入的"参数 vs 目标"失配场景在测试中被完整验证(如 4 个去耦 → 目标 6 个,自动收敛)。

---

## 10. 管线 B:知识驱动的 17 步 PCB 全流程

`orchestration/pipeline.py` 固定一条行业标准流程,把设计推进到布局与布线。

### 固定的 17 步(`PipelineStep` 枚举,顺序即权威)

```
1  requirements           需求解析(归一化为 RequirementSpec)
2  topology               拓扑设计(功能块 + 供电轨 + 地)
3  selection              元件选型(符号/封装接地;MPN/LCSC 仅来自真实目录)
   —— 原理图 ——
4  schematic_connections  连接设计(逻辑网表 NetlistIntent)
5  schematic_pinmap       引脚映射(逻辑引脚→真实器件引脚号)
6  schematic_layout       原理图布局(sheet 摆放 + 标签/局部线)
7  schematic_materialize  物化 .kicad_sch(round-trip 计数校验)
8  erc                    ERC 兜底(确定性短路/单脚网 + 可选 kicad-cli ERC)
   —— 布局 ——
9  layout_partition       分区/板框(功能区 zone)
10 layout_critical        关键器件约束摆放(晶振靠 MCU、去耦就近、连接器沿边)
11 layout_general         常规摆放 + 对齐(尺寸感知装箱)
12 layout_write           兜底(重叠/越界)+ 写 .kicad_pcb
   —— 布线 ——
13 route_plan             叠层 + 网络分类(NetClass 线宽/间距/过孔)
14 route_planes           电源地平面 + 关键网优先
15 route_signals          常规布线(Freerouting;缺失时降级 deferred)
16 route_fab              工艺兜底审计(线宽/间距 vs 工艺最小值)
   —— 制造 ——
17 manufacture            DRC 兜底 + 导出 BOM / CPL /(有 kicad-cli 时)Gerber
```

### 每步统一形态(`PipelineStepBase`)

```
按 role 检索该步软知识(top_k=3)
  → propose():LLM 结构化提议(经该步 Pydantic 合同校验)或确定性 fallback
  → check():廉价"防烧板"兜底,只查真实库 / 工艺表
  → 任一 ERROR 级 CheckResult 未通过 → 该步 blocked(fail closed)
```

- **`propose_structured`** 决定 LLM/兜底取舍:`offline` 直接用 fallback;`auto` 任何提议失败
  (缺 client / 请求失败 / 输出非法)都回退 fallback;`required` 则 **fail closed** 抛 `LlmError`。
- **步骤顺序不可跳、不可乱**:`Pipeline` 校验注册的步骤必须等于 `CANONICAL_ORDER` 的前缀,
  否则抛 `PipelineOrderError`;执行**停在第一个 blocking 步**。
- 每步产出存入 `PipelineState.artifacts[step]`,供后续步骤消费;`PipelineContext` 携带
  `mode` / `client` / `kb` / `out_dir`。

### 每步的输入输出合同(`pipeline_contracts.py`)

均继承 `ContractModel`(`extra="forbid"`),LLM 只能*提议*、构造即校验结构:

`TopologyPlan`(blocks/rails/ground)、`SelectionPlan`(`SelectedPart`,mpn/lcsc 仅来自真实
目录、缺失留空)、`NetlistIntent`(`LogicalPin` 逻辑网)、`PinMapPlan`(`MappedPin` 真实引脚号
+ unresolved)、`SchLayoutPlan`、`MaterializeResult`、`ErcSummary`、`BoardPartition`
(`BoardZone`)、`PcbPlacementPlan`(`PcbPlacement`)、`PcbWriteResult`(overlaps/out_of_bounds)、
`RoutePlan`(`NetClass`)、`PlanePlan`、`RouteResult`(method = freerouting/deferred/manual)、
`FabAudit`、`ManufactureResult`。

### 分工再强调

**LLM 做设计决策**(读懂/选型/布局/布线策略);**兜底校验只查真实库/工艺表**
(引脚/焊盘是否存在、线宽间距是否 ≥ 工艺最小值、短路/单脚网/重叠/越界),
**绝不编码业务规则**。LLM 不可裁定,兜底不做设计。

---

## 11. 三个 Agent 角色

三个角色由同一个 EricAI 模型 + 不同角色提示 + 不同输出合同实现;多智能体能力来自
**角色隔离、上下文隔离、权限隔离、结构化交接**。定义在 `agents/`,LLM 抽象在 `agents/llm.py`
(`LlmMode`、`LLMClient` 协议、惰性 `EricAIClient`、`resolve_client`、`parse_mode`、`LlmError`)。

- **Architect(`architect.py`)** —— 需求归一化 + 族判断 + 参数选择 + 澄清追问。LLM 输出的
  参数**经 `Atmega328Params` 合同重新校验**;判为其他族即不合格;`required` fail closed,
  `auto` 回退确定性(离线走 `heuristics.py` 的关键词提取)。
- **Reviewer(`reviewer.py`)** —— 有 finding 时生成可读审查叙述并对 finding **分诊**
  (疑似误报 / 优先级,仅辅助)。严重级别**始终取自原始 finding**,LLM 无法降级真实错误;
  无 finding 时直接返回确定性空审查、不调用模型。产出 Markdown。
- **Coder(`coding.py`)** —— gate 失败时做**根因诊断**并在白名单内提议修复。唯一允许的操作是
  `set_param`(仅限 `Atmega328Params` 字段);任何非白名单操作(如 `run_shell`)一律拒绝并
  fail closed;不能写文件、不能跑 shell。

---

## 12. 两层知识:硬事实与软知识

**第一层——硬事实(权威,不进向量库):**

- 电路族定义与跨参数规则(`families/`)、gate 阈值与公式(`verification/`);
- 真实符号引脚 / 封装焊盘几何(`eda/symbols.py`、`eda/footprints.py`);
- 工艺最小值表(`data/process_capability.json` + `config.ProcessCapability`);
- 真实元件目录(本地 JLCPCB 缓存,`parts/selector.py`)。

精确查表 / 计算,**不进向量库**,LLM 不能编造。

**第二层——软知识(顾问,进检索库):** `knowledge/corpus/*.md`(16 篇),按流程阶段用
`role:` 标签组织(topology / selection / schematic / layout / routing / stackup / dfm / emc):

```
component_selection  crystal            decoupling          dfm_manufacturing
emc_grounding        error_taxonomy     impedance_stackup   ldo
net_design           placement_constraints  placement_partitioning  power_tree
reset                schematic_readability  trace_width_current      vias_return_path
```

由 `knowledge/store.py` 的 `KnowledgeBase` 索引,`build_default_kb()` 构建默认库:

- 有 EricAI embedder 时:embedding(bge-m3)+ 余弦相似度 +(可选)reranker;
- 离线时:无依赖的词法检索**优雅降级**,Agent / 管线仍可运行。

`retrieve(query, top_k, role)` 供各角色与管线每步按其 `role` 检索相关片段注入 prompt
(仍走下游校验 / 兜底)。你收集的设计经验可直接补进 `corpus/`(散文式经验,不含精确唯一真值)。

---

## 13. EDA 适配层与真实库

- **`eda/adapter.py`** —— `SchematicDoc` 类型化门面 + `run_erc`(封装 kicad-cli)。
- **`eda/materialize.py`** —— `materialize_design(ir, board, supply_net)`:IR + BoardPlan →
  真实 `.kicad_sch`(可在 KiCad 10 打开编辑)。
- **`eda/symbols.py`** —— 解析 `lib_id` → 真实引脚(号 / 名 / 坐标),支持 KiCad 10 新的
  `<nick>.kicad_symdir/<symbol>.kicad_sym` 目录格式与**跨文件 `extends` 继承**
  (如 `ATmega328P-A` 继承 `ATmega48PV-10A`),并兼容旧单文件 `<nick>.kicad_sym`。
- **`eda/footprints.py`** —— 解析 `lib_id` → 真实焊盘 / 包围盒(供重叠 / 越界兜底)。
- **`eda/vendor/`** —— vendored 的 `kicad-mcp-py` 核心(进程内、类型化,**不走 MCP**),
  含 `sexpr / schematic / pcb / connectivity / footprint / symbol_lib / library /
  kicad_cli / kicad_paths / review / jlcpcb / fsutil`。

---

## 14. 审查任意 KiCad 工程

`review_project(project_path, mode)`(CLI `review`)复用 vendored 的 kicad-happy 式审计
(直接解析 `.kicad_sch` / `.kicad_pcb`):连通性、去耦、电源轨、BOM 健康、(有 PCB 时)
可制造性。审计结果归一化为 Finding → 顾问性 gate(非发布 gate)→ 交给 Reviewer 产出 Markdown。
这条能力**独立于生成**,你自己画的板也能审。

---

## 15. 接地选型

`parts/selector.py`(CLI `parts`)在本地 JLCPCB SQLite 缓存上做**接地**查询——元件型号必须
来自真实目录,**不允许 LLM 凭记忆编造 MPN**。缓存缺失时 `available()==False`、返回空
(绝不编造),Agent 仍可运行。缓存路径遵循 vendored 默认(`KICAD_MCP_HOME/jlcpcb.sqlite`);
设置 `KICAD_MCP_HOME` 指向你自己的缓存。`ground_ir(ir)` 可按元件 value + 封装为每个元件
给出候选 LCSC 器件;管线 B 的选型步也复用它接地 MPN/LCSC。

---

## 16. 产物与运行目录

**管线 A(`design ... --out runs/demo`)**:

- `plan.json` —— 不可变 `DesignPlan`(需求 + Circuit IR + BoardPlan + 参数),审批边界;
- `<project>.kicad_sch` —— 生成的原理图(可在 KiCad 打开编辑);
- `<project>.kicad_pro` —— 最小工程文件(GUI 便利);
- `gate_report.json` —— `VerificationReport`(gate、finding、指标)。

**管线 B(`pcb ... --out runs/pcb --project board`)**:

- `<project>.kicad_sch` / `<project>.kicad_pcb` —— 原理图与 PCB;
- `<project>_bom.csv` / `<project>_cpl.csv` —— BOM 与贴装坐标;
- `gerber/` —— 装有 kicad-cli 时导出的 Gerber。

运行产物、`.env`、本地缓存不应提交 Git(见 `.gitignore`)。

---

## 17. 测试与质量门禁

```powershell
# 纯 Python 合同/单元测试(不启动真实 EDA)
.\.venv\Scripts\python.exe -m pytest -q -m "not real_kicad"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src

# 在装好 KiCad 10 的机器上运行可选真实 ERC 测试
$env:RATSNESTPRO_RUN_REAL_KICAD_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q -m real_kicad
```

当前:**143 个测试全部通过**(26 个测试文件,含可选 `real_kicad`);**ruff 全部检查通过**;
**mypy 在 46 个源文件上零告警**。测试覆盖:领域合同、EDA 适配、真实符号库、验证、电路族、
配置/工艺表、语料 role、生成编排、Agent(architect/reviewer/coder)、修复、知识库、独立审查、
接地选型,以及 17 步管线的框架与逐步(topology / selection / connections / pinmap /
sch-layout / materialize / erc / layout-partition / layout-place / routing / manufacture /
端到端)。真实 KiCad 路径以 `real_kicad` 标记,需装 KiCad 10 时才跑。

---

## 18. 实现状态与能力边界

**已实现:**

- **管线 A**:族内参数化生成、确定性 gate、半自动/自动修复循环。
- **管线 B**:完整 17 步知识驱动流程(需求→拓扑→选型→原理图→布局→**布线**→制造),
  每步 LLM 提议 + 确定性兜底,真实符号/封装库与工艺表接地。
- **Agent**:EricAI 三角色(带 `auto` 回退 / `required` fail-closed 模式)。
- **知识库**:按 role 的检索(EricAI embedding+reranker / 离线词法回退)。
- **独立能力**:任意 KiCad 工程审查、本地 JLCPCB 接地选型。

**未来工作:** MCP 对外暴露;更完整的自动布线(现依赖外部 Freerouting)。

**明确的能力边界(务必知晓):**

- 管线 A 的生成仅在 **ATmega328 电路族内**可靠,**不是**通用自然语言→任意电路生成器;
  管线 B 更通用,但仍以真实库覆盖度与工艺兜底为限。
- **布线**依赖外部 **Freerouting**;缺失时优雅降级为 `deferred`(记录未完成、不阻断)。
- 兜底是"防烧板"级的**廉价物理/存在性检查**(引脚/焊盘存在、线宽间距 ≥ 工艺最小值、
  短路/单脚网/重叠/越界),**不替代**完整 DRC / SI / EMC sign-off。
- 确定性布局是**可制造的 baseline**(尺寸感知装箱,保证无重叠 / 在框内);紧密、美观的
  布局由 LLM(`auto`/`required`)完成。
- **不生成固件、不生成机械外壳**;**不做** SPICE / 热 / EMC 认证 sign-off。
- 审查是**辅助发现问题**,最终工程判断归人。
- 知识库降低幻觉但**不替代**确定性验证 / 兜底;硬事实不进向量库。
- kicad-cli 缺失时 ERC / Gerber 报 `unavailable`(WARNING),确定性 gate / 兜底为权威。
- **已知局限:** vendored 写入器未嵌入符号引脚几何,因此真实 kicad-cli ERC 可能报未连接
  引脚——离线流程以**确定性 gate / 兜底为权威**;ERC 干净取决于符号图形嵌入,仅在
  `real_kicad` 标记下考察其"能运行"。

---

## 19. 溯源与许可证

- EDA 适配层 vendored 了 `kicad-mcp-py` 核心(MIT)——`sexpr / schematic / pcb /
  connectivity / footprint / symbol_lib / library / kicad_cli / kicad_paths / review /
  jlcpcb / fsutil`;MCP 外壳未 vendored。
- 审查复用 `kicad-happy` 式分析器方法(MIT)。
- 架构范式与 ATmega328 参考电路遵循 `RatsNest agent-runtime-v2` 的设计。

许可证:MIT。
