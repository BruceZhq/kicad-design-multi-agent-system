# RatsNestPro 五案例真实端到端测试报告

测试日期：2026-07-30（Asia/Shanghai）

## 结论

当前版本已经证明：

- 前端所依赖的 FastAPI + SSE + LangGraph 运行链路能够稳定承载多个长任务；
- 5/5 个自然语言请求被正确识别为 `build`，没有误进入 `review`；
- 5/5 个服务请求正常结束，没有服务异常、超时或状态串扰；
- 3/5 个任务贯彻了“优先产出”的策略，完成 17/17 步并生成可编辑 KiCad 原理图、PCB、BOM/CPL、Gerber/钻孔和独立审查报告；
- 系统没有把这些存在严重电气问题的草案虚报成发布成功。

但当前版本尚未达到“任意合理 KiCad 需求都能完整完成并可制造”的生产通用化目标：

- `release_ready`：0/5；
- 真实 Freerouting 成功：0/5；
- DSN/SES 成对生成并成功导回：0/5；
- 2/5 因通用 Harness 能力缺口在早期停止；
- 3 个完成 17 步的工程均存在严重连接、器件语义或封装问题，Gerber/钻孔只能视为诊断草案，不能用于投板。
- 三个最终 `.kicad_pcb` 与各自的 `.unrouted.kicad_pcb` SHA-256 完全相同，且 PCB 中的 `segment`、`via`、`zone` 数均为 0，证明它们没有被实际布线。

因此，本轮结果应定义为：**服务编排与“产物优先、诚实报告”策略基本成立，通用硬件综合质量仍不合格。**

## 测试方法

- 入口：公开的 `POST /ratsnestpro-multi-agent/stream` SSE 接口；
- 未强制设置 `workflow_mode`，把意图识别纳入测试；
- 每个案例使用唯一的 `thread_id`、`request_id`、`project_name` 和 `run_name`；
- 并发度为 2，防止 DeepSeek 限流及 KiCad/Freerouting 资源竞争；
- 生产 Harness 在整个测试期间冻结，没有针对某个案例边跑边修改；
- 结果以磁盘上的 `pipeline_result.json`、KiCad 文件、制造文件和 Reviewer 报告为准，不能用 HTTP `completed` 代替硬件验收；
- 五个请求总墙钟时间约 39 分 15 秒。

测试需求定义：

- [`five-case-e2e-2026-07-30.json`](../test-cases/five-case-e2e-2026-07-30.json)

可复现执行器：

- [`run_ratsnest_e2e_matrix.py`](../../../scripts/run_ratsnest_e2e_matrix.py)

原始 SSE 事件及请求：

- `data/ratsnestpro/e2e-tests/ratsnest-five-case-20260730/`

说明：首次执行器把等待本地并发信号量的时间计入了后续案例的 `duration_seconds`。本报告使用服务运行注册表中的 `started_at`/`finished_at` 计算实际执行时间；执行器已改为分别记录排队、活动和总墙钟时间，原始测试证据未被改写。

## 五个完整需求实例

| ID | 设计域 | 主控/核心器件 | 主要覆盖能力 |
|---|---|---|---|
| 01 | 便携式环境记录器 | RP2040 | USB-C、LiPo power-path、QSPI、microSD、BME280、LIS3DH |
| 02 | 24 V 三相 BLDC 控制器 | STM32G431CBT6 | 三相栅极驱动、6 MOSFET、电流采样、CAN FD、工业电源 |
| 03 | 隔离 Modbus/Wi-Fi 网关 | ESP32-C3-WROOM-02U | 双路隔离 RS485、隔离电源、继电器、USB-C、RF |
| 04 | BLE 运动与环境信标 | nRF52840-QIAA | 裸片射频、晶振、LiPo、USB、IMU、电量计、本地库生成 |
| 05 | USB/DIN MIDI 控制面板 | STM32F072CBT6 | USB MIDI、DIN MIDI、按键矩阵、编码器、ADC 推子、OLED |

这组案例有意覆盖不同 MCU 厂商、封装、电源域、隔离域、模拟、功率、RF、USB 和人机接口，未使用 ATmega 模板，也不是同一块板的改名变体。

## 结果矩阵

| ID | 服务状态 | 硬件结果 | 进度 | 实际活动时间 | `.kicad_sch` / `.kicad_pcb` | BOM/CPL | Gerber/钻孔 | DSN/SES | ERC error | DRC error | Reviewer |
|---|---|---|---:|---:|---|---|---|---|---:|---:|---|
| 01 | completed | completed_with_issues | 17/17 | 1605.4 s | 是 / 是 | 是 | 是 | 否 / 否 | 112 | 20 | 已运行，blocked |
| 02 | completed | completed_with_issues | 17/17 | 1002.8 s | 是 / 是 | 是 | 是 | 否 / 否 | 37 | 9 | 已运行，blocked |
| 03 | completed | execution_blocked | 1/17 | 335.1 s | 否 / 否 | 否 | 否 | 否 / 否 | 未运行 | 未运行 | 未运行 |
| 04 | completed | execution_blocked | 0/17 | 28.7 s | 否 / 否 | 否 | 否 | 否 / 否 | 未运行 | 未运行 | 未运行 |
| 05 | completed | completed_with_issues | 17/17 | 988.5 s | 是 / 是 | 是 | 是 | 否 / 否 | 75 | 8 | 已运行，blocked |

这里的 Reviewer `blocked` 表示独立审查发现发布阻断项，不表示 Reviewer 工具没有运行。案例 01、02、05 均存在非空审查报告。

所有标记为“存在”的产物都经过非空检查。三个完整草案分别导出了 18 个 Gerber 类文件和 2 个钻孔文件；BOM/CPL 也能作为 CSV 正常解析：

- RP2040：126 / 125 行；
- STM32G431：74 / 74 行；
- STM32F072：108 / 108 行。

四个已有的 `pipeline_result.json` 都没有显式 `release_ready` 字段；表中的“不通过”由 `outcome` 和 `release_blockers` 推导。这是一个结果 Schema 完整性问题，后续应让每个结果都显式给出布尔发布状态。

三个 DRC 报告中的 `unconnected=0` 也不能解释为已经连通：同一结果中的权威路由字段明确为 `assigned_pads=0`、`routed_tracks=0`、`unconnected=-1`，且实际 PCB 没有任何走线。结果汇总必须规定证据优先级，不能让局部 DRC 计数覆盖路由失败事实。

## 多智能体链路覆盖

| 节点/能力 | 覆盖数 | 结果 |
|---|---:|---|
| Intent Router | 5/5 | 全部正确识别为新建设计 `build` |
| Architect | 5/5 | 均调用内部知识、KiCad 官方资料、真实库查询、Web/数据手册工具 |
| Parts Specialist | 4/5 | 本地采购缓存不可用；如实报告且未编造库存 |
| Hardware Engineer | 4/5 | 3 个完成 17 步，1 个在 RequirementSpec 阶段执行阻断 |
| Reviewer | 3/5 | 对三个实际 KiCad 工程完成独立审查 |
| Supervisor | 5/5 | 汇总实际状态，未将草案冒充为发布成功 |

案例 04 在 Architect 阶段停止，因此没有进入 Parts Specialist、Hardware Engineer 和 Reviewer。

## 逐案例审计

### 01 — RP2040 便携环境记录器

正向结果：

- 正确选中真实 `MCU_RaspberryPi:RP2040` 和 QFN-56 封装；
- 完成 17 步并生成原理图、未布线 PCB、BOM/CPL、Gerber/钻孔及 Reviewer 报告；
- KiCad CLI 的 ERC/DRC 均真实执行。

主要问题：

- 将 `Memory_Flash:W25Q128JVE` 显示为 `W25Q64JV`，属于器件身份不一致；
- 两针 JST-PH 电池接口 `J2` 实际被映射成 `Conn_02x38_Odd_Even` 和 76 针排母封装；
- 选择结果中出现 59 个 `sensor_decoupling` 电容，暴露了器件数量展开失控；
- microSD 连接器语义/引脚模型错误，产生大量无意义未连接脚；
- SWD、复位、电源 mux、传感器控制脚和 LED 网络存在多网占用、单端网络及虚构元件引用；
- 路由入口发现逻辑引脚属于多个网络，因而未生成 DSN/SES，Freerouting 没有执行。

证据：

- `data/ratsnestpro/runs/rp2040-portable-env-logger-e2e/pipeline_result.json`
- `data/ratsnestpro/reviews/rp2040-portable-env-logger-e2e-review.md`

### 02 — STM32G431 BLDC 控制器

正向结果：

- 选中真实安装库中的 STM32G431CBTx 系列符号和 LQFP-48；
- 完成 17 步并生成可编辑工程、制造草案和 Reviewer 报告；
- ERC/DRC 真实执行。

主要问题：

- 工业输入保护中的 `SS34` 实际仅 40 V，低于需求定义的 45 V 最低裕量；
- 需求指定精确料号 `STM32G431CBT6`，而落地 BOM/设计身份仍是通配变体 `STM32G431CBTx`，精确器件身份没有闭合；
- `U4` 显示为三相栅极驱动器 `DRV8353RS`，实际符号却是双 H 桥 `Driver_Motor:DRV8833RTY`，并配 16 引脚 DRV8833 封装；
- 三相电机输出 `J9` 实际是名为 `MOTOR_2pin` 的两针连接器；
- buck 开关节点、三相栅极驱动、相输出、分流采样、CAN、Hall 和外部 I/O 出现大量引脚多网冲突；
- SWD NRST、VBAT、CAN TVS 和连接器约束不完整；
- 路由在 DSN 导出前被多网冲突拒绝，Freerouting 未执行；
- 高电流铜、温升和热设计本就需要人工工程复核，这属于合理人工边界，但当前工程在进入该边界之前已有确定性连接错误。

证据：

- `data/ratsnestpro/runs/stm32g431-bldc-controller-e2e/pipeline_result.json`
- `data/ratsnestpro/reviews/stm32g431-bldc-controller-e2e-review.md`

### 03 — ESP32-C3 双路隔离网关

正向结果：

- 意图识别和 Architect 资料检索链路正常；
- Parts Specialist 在缺少采购缓存时没有编造数据；
- 执行阻断被如实记录为 Harness capability gap。

直接阻断原因：

- Hardware Engineer 生成的结构化 `RequirementSpec` 在约 23,580 字符处截断；
- Pydantic 报 `Invalid JSON: EOF while parsing a string`；
- 有界结构化输出修复/重试没有恢复，因此在 1/17 停止。

附加通用问题：

- 用户要求 `ESP32-C3-WROOM-02U`，本地库同时存在 `...-02` 和精确的 `...-02U`，但候选评分把非 U 版本排在精确版本之前；这暴露了精确身份匹配优先级错误。

证据：

- `data/ratsnestpro/runs/esp32c3-isolated-modbus-gateway-e2e/pipeline_result.json`

### 04 — nRF52840 裸片 BLE 信标

正向结果：

- 意图识别正确；
- Architect 实际查询了内部知识、KiCad 官方文档、安装库和 Nordic 资料；
- 没有用模块或其他 nRF52 改名冒充。

直接阻断原因：

- 安装库没有精确的 `nRF52840-QIAA`；
- 用户已经允许依据 Nordic 官方资料创建项目本地符号/封装，但当前系统没有通用的“本地符号/封装生成、映射、验证、注册”能力；
- Architect 因 `no grounded KiCad symbol` 在 0/17 停止。

这不是合理的硬件硬约束冲突，而是明确的 Harness 能力缺口；它还没有被跨 Architect/Pipeline 的 EHE 正确记录。

证据：

- `data/ratsnestpro/e2e-tests/ratsnest-five-case-20260730/04-nrf52840-motion-beacon/`

### 05 — STM32F072 USB/DIN MIDI 控制面板

正向结果：

- 正确选中 STM32F072CBT6 和 LQFP-48；
- 完成 17 步并生成 KiCad、BOM/CPL、制造草案和独立审查；
- 复杂的 16 键矩阵没有导致流程提前停止。

主要问题：

- 编码器拓扑没有映射为真实编码器器件，错误使用带 `AC/L`、`AC/N` 电源语义的符号；
- 四个编码器显示为 `RotaryEncoder_Alps_EC12E`，但其真实符号和封装均来自 `Converter_ACDC:IRM-20-12` AC/DC 电源模块；
- 两个 DIN-5 MIDI 连接器虽然使用 DIN 显示值/符号，却配成 Infineon PG-TDSON-8 功率封装；
- OLED 接口出现无法解析的 `?` 引脚；
- I²C、矩阵列、SWO、编码器、ADC 推子和 MIDI 光耦存在 MCU/器件引脚多网冲突；
- LED 限流网络、VBAT 和多个连接器/小器件连接不完整；
- 路由在 DSN 生成前失败，Freerouting 未执行。

证据：

- `data/ratsnestpro/runs/stm32f072-usb-midi-control-surface-e2e/pipeline_result.json`
- `data/ratsnestpro/reviews/stm32f072-usb-midi-control-surface-e2e-review.md`

## Reviewer 一致性问题

三份 Reviewer 文件顶部的确定性权威结论都是 `BLOCKED`，并正确列出了 ERC/DRC 与发布阻断项；但报告后部的 LLM advisory narrative 又出现 `PASS (deterministic gates)` 和 `Blockers: None`。Supervisor 最终采用了权威 `BLOCKED`，所以本轮没有错误放行，但同一报告内的相反陈述会直接误导用户。

Reviewer 输出应只允许一种最终裁决来源：确定性 gate 生成不可覆盖的结论，LLM 只能解释问题和提出修复建议，不能再次输出独立的 PASS/BLOCKED 判定。

## AHE / EHE 评价

本轮体现了正确的一面：

- 三个存在设计错误但仍可机械继续的案例没有被早期 `blocked`，而是产出完整草案并标记 `completed_with_issues`；
- AHE 没有围绕普通硬件错误长时间自循环，三个完整案例的 `repair_attempts` 均为 0，避免了不可控耗时和 token 消耗；
- ESP32 案例把结构化输出失败登记为 capability gap。

仍然缺失的部分：

- JSON 截断属于可恢复 Harness 故障，却没有触发最小化重问、JSON 续写或分段 RequirementSpec 重建；
- nRF52840 的本地库生成缺口发生在 Architect 层，没有进入统一 AHE/EHE 账本；
- EHE 当前主要“记录”，尚未在后续运行前提供经过验证的能力补丁；
- 三个完整案例在进入路由前已经有确定性多网冲突，但没有一次预算受限的通用“资源重分配 + 拓扑重建”修复。
- 个别 run 的本地 `capability_gaps=0`，但 `pipeline_result.ehe.candidate_gaps` 带入了其他历史项目的候选项；这更像全局 EHE 池快照而不是已证实的并发状态竞争，但必须显式区分 `run_local_gaps` 与带版本/来源的 `global_ehe_candidates`。

## 通用根因排序

### P0 — 决定能否形成可布线工程

1. 在网络生成前建立全局 MCU/连接器/外设引脚资源分配器，保证一个物理引脚只承担兼容功能。
2. 用真实符号引脚表验证并规范化网络，不允许 `?` 引脚、虚构元件引用或同一引脚进入不同网络。
3. 建立语义硬件块到真实器件族/子电路模板的可验证映射，优先覆盖 microSD、编码器、开关电源、CAN/RS485、USB 和功率级。
4. 精确 MPN/variant 匹配必须高于模糊相似度；禁止非 U/U、容量、耐压或型号变体被显示值掩盖。
5. 在 DSN 导出前进行一次有界的通用连接修复；无法修复时仍保留草案，但不得声称执行了 Freerouting。
6. 统一结果证据优先级：实际文件与路由事实高于局部 DRC 指标，确定性 Reviewer gate 高于 LLM advisory 文本。

### P1 — 消除非设计性早期阻断

1. 对截断或无效结构化 LLM 输出实施有界恢复：缩小 schema、分块生成、针对性 JSON repair、单次最小重问。
2. 增加项目本地 KiCad 符号/封装生成流水线：官方证据 → 引脚/焊盘表 → 生成 → pin/pad 校验 → 临时库注册 → KiCad CLI 验证。
3. 将 Architect、Parts、Pipeline、Reviewer 的 capability gap 统一进入 EHE，而不是只覆盖 Pipeline。
4. 让 Reviewer 的 LLM 段落消费并解释确定性裁决，禁止生成第二个相互矛盾的裁决。

### P2 — 生产能力与效率

1. 配置真实、可更新的采购数据缓存；不可用时继续设计，但发布门必须保持未验证。
2. 把 Reviewer 的长 Web 查询按错误类别拆分，避免过长查询返回空结果。
3. 缓存真实库索引与器件解析结果，减少 70–100+ 器件工程反复全库扫描。
4. 对复杂布局和 Java Freerouting 设置独立资源配额与超时，不把服务总超时与路由超时设为同一上限。
5. 全局 EHE 经验池使用原子版本/CAS 和来源标识；只有通过独立回归的修复才能晋升为已验证经验。

## 最终判定

| 能力 | 本轮判定 |
|---|---|
| 服务稳定性、SSE、状态隔离 | 通过 |
| 意图识别 | 通过本轮 5 个 build 案例 |
| 多智能体链路真实调用 | 通过，但早期缺口会截断下游节点 |
| 产物优先策略 | 部分通过，3/5 完成 17 步 |
| 诚实状态与证据报告 | 通过 |
| 通用器件/拓扑综合 | 不通过 |
| 可恢复 Harness 故障的 AHE | 不通过 |
| 跨节点 EHE 闭环 | 不通过 |
| Freerouting 与发布门 | 不通过 |
| 可制造发布质量 | 不通过，0/5 release-ready |

本轮测试没有证明系统“只能做某几类板”；相反，它证明了控制流已经能覆盖多种板型。真正限制通用性的共同瓶颈在于：**器件语义落地、全局引脚分配、真实符号/封装能力和结构化 LLM 输出恢复**。这些应作为下一阶段的通用 Harness 工作，而不是针对上述五块板写专用条件。
