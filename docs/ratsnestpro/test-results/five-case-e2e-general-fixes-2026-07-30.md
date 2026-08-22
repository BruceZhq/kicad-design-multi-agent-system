# RatsNestPro 五案例 E2E `generalfix-v6` 独立审计

审计日期：2026-07-30

## 结论

`generalfix-v6` 证明了系统已经能够在存在设计问题时优先保留产物并继续执行，但尚未证明能够稳定生成可制造 PCB。

- SSE 传输完整：5/5。
- 进入并走完 Hardware Engineer 17 步流水线：4/5。
- 实际生成非空 `.kicad_sch` 和 `.kicad_pcb`：4/5。
- 实际生成非空 Freerouting `.dsn` 和 `.ses`：2/5。
- Freerouting 达到零未连接：0/5。
- 同时通过 KiCad ERC 和 DRC：0/5。
- `release_ready=true`：0/5。

因此，4 个 `completed_with_issues` 只表示“流水线执行完毕并保留了可编辑草稿及制造中间文件”，不表示设计正确、布线完成或可直接投产。所有 Gerber、钻孔、BOM、CPL 都必须视为诊断产物，不能交付制造。

## 审计口径与数据来源

本报告只读取以下 v6 证据：

- `data/ratsnestpro/e2e-tests/ratsnest-five-case-20260730-generalfix-v6/matrix.json`
- `data/ratsnestpro/e2e-tests/ratsnest-five-case-20260730-generalfix-v6/stream_summary.json`
- 各案例目录下的 `events.json`、`final_message.json`、`stream_result.json`
- `data/ratsnestpro/runs/*generalfix-v6/pipeline_result.json`
- 上述 run 目录内实际存在且文件大小大于 0 的产物

判定层级严格分开：

1. **SSE 完整**：服务端流正常结束，只证明请求和事件流没有中途断开。
2. **17 步执行完成**：`completed_steps=17` 且 `execution_complete=true`，只证明流水线走到末尾。
3. **Freerouting 完成**：必须实际存在非空 DSN、SES，并且路由结果的未连接数为 0。
4. **ERC/DRC 通过**：两项都必须实际运行且 error 为 0；DRC 的 unconnected 也必须为 0。
5. **可发布**：只能由 `release_ready=true` 表示。

`completed_with_issues` 不会被计作“成功制造板”。

## 总览

时间均来自 `stream_summary.json`。执行时长是不含排队的案例处理时间；墙钟时长从入队开始计算。

| 案例 | SSE | 执行时长 | 墙钟时长 | 流水线 | Freerouting | ERC | DRC | `release_ready` |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| RP2040 portable env logger | 完整 | 46m 50.2s | 46m 50.2s | 17/17 | 生成 DSN/SES；结构化结果 0/51 nets，52 unconnected | 96 errors / 238 warnings | 64 errors / 414 warnings / 52 unconnected | false |
| STM32G431 BLDC controller | 完整 | 10m 47.4s | 10m 47.4s | 17/17 | `error`；未生成 DSN/SES | 30 errors / 150 warnings | 0 errors / 179 warnings / 0 unconnected | false |
| ESP32-C3 isolated Modbus gateway | 完整 | 17m 06.2s | 27m 53.6s | 17/17 | 生成 DSN/SES；46/47 nets，1 unconnected | 46 errors / 173 warnings | 19 errors / 239 warnings / 1 unconnected | false |
| nRF52840 wearable motion beacon | 完整 | 4m 28.3s | 32m 21.9s | 0/17 | 未到达 | 未运行 | 未运行 | 无 pipeline result；最终状态 `EXECUTION_BLOCKED` |
| STM32F072 USB MIDI control surface | 完整 | 14m 55.1s | 47m 16.9s | 17/17 | `error`；未生成 DSN/SES | 29 errors / 215 warnings | 3 errors / 284 warnings / 0 unconnected | false |

STM32G431 和 STM32F072 的 DRC `unconnected=0` 不能解释为布线成功：两者的 Freerouting 都在 pin-map/footprint mismatch 阶段报错，且没有实际 DSN/SES。v6 在不同阶段、不同对象粒度上汇总 route 与 DRC 指标，这也是后续 “route metric same-grain” 修复要解决的问题。

## 实际非空产物

下表只统计真实存在且大小大于 0 的文件。PCB 数量为 2 时，包含当前 `.kicad_pcb` 和 `.unrouted.kicad_pcb`。

| 案例 | SCH | PCB | DSN | SES | ERC 报告 | DRC 报告 | BOM | CPL | Gerber/钻孔 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| RP2040 | 1 / 220.8 KiB | 2 / 929.8 KiB | 1 / 65.4 KiB | 1 / 48.9 KiB | 1 / 204.4 KiB | 1 / 299.6 KiB | 1 / 5.7 KiB | 1 / 5.8 KiB | 23 / 966.6 KiB |
| STM32G431 | 1 / 162.2 KiB | 2 / 462.6 KiB | 0 | 0 | 1 / 110.7 KiB | 1 / 102.6 KiB | 1 / 3.4 KiB | 1 / 3.3 KiB | 23 / 538.7 KiB |
| ESP32-C3 | 1 / 190.9 KiB | 2 / 730.5 KiB | 1 / 91.0 KiB | 1 / 56.3 KiB | 1 / 134.7 KiB | 1 / 152.7 KiB | 1 / 4.1 KiB | 1 / 1.8 KiB | 25 / 793.8 KiB |
| nRF52840 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| STM32F072 | 1 / 216.7 KiB | 2 / 854.7 KiB | 0 | 0 | 1 / 150.5 KiB | 1 / 168.7 KiB | 1 / 5.0 KiB | 1 / 3.8 KiB | 23 / 843.7 KiB |

制造文件存在并不表示制造检查通过。此处统计的用途是验证“artifact-first”策略确实保留了中间结果，而不是给出投产许可。

## 分案例审计

### 1. RP2040 portable env logger

执行层面：

- SSE 完整，17/17 步完成。
- Freerouting 确实被调用，DSN 和 SES 均为非空文件。
- `pipeline_result.json` 报告 678 tracks、0/51 routed nets、52 unconnected。
- Freerouting note 中出现 “50 unrouted”，而结构化结果和 DRC 都报告 52 unconnected；这是 v6 路由统计粒度不一致的直接证据。

主要失败根因：

- 电源输入/输出极性、电源源头、双电源防反灌等连接不完整。
- SWD、MCU reset/boot 和部分器件引脚未正确闭合。
- 存在未使用器件、单引脚网络、未放置器件和局部支撑器件距离过远。
- ERC 96 errors，DRC 64 errors，最终仍有 52 个未连接项。

结论：产生了完整的可编辑草稿和 Freerouting 中间文件，但路由远未完成，不能制造。

### 2. STM32G431 BLDC controller

执行层面：

- SSE 完整，17/17 步完成。
- Freerouting 在进入有效路由前失败：`matched 144/165 logical pins (145 physical pads assigned)`。
- 实际没有 DSN 或 SES。
- DRC 虽报告 0 errors、0 unconnected，但它检查的不是成功导回 Freerouting 结果，因此不能抵消路由失败和 ERC 失败。

主要失败根因：

- 多个关键器件使用了不存在的库符号，或存在显示值冒充器件、symbol/footprint pin-pad 不兼容。
- 栅极驱动器、CAN、电源器件的逻辑引脚无法映射到真实符号。
- SWD、CAN 终端/TVS、buck 核心拓扑、MCU reset/boot 等连接不完整。
- 最终 pin-map/footprint mismatch 阻止了 DSN/SES 生成，ERC 仍有 30 errors。

结论：17 步“走完”是容错执行完成，不是路由完成；当前文件只能作为问题定位草稿。

### 3. ESP32-C3 isolated Modbus gateway

执行层面：

- SSE 完整，17/17 步完成。
- 实际生成 DSN 和 SES，Freerouting 生成 764 tracks。
- 路由结果为 46/47 nets，仍有 1 unconnected。
- ERC 46 errors；DRC 19 errors、1 unconnected。

主要失败根因：

- 存在器件身份/耐压证据问题和缺失库符号。
- SWD、RS485 终端、外部 I²C、buck、电源输入保护链等连接或接口语义不完整。
- 存在未连接网络、未放置器件、未嵌入符号图形以及间距、孔径等 DRC 问题。

结论：这是 v6 中最接近完整路由的案例，但仍未达到 ERC/DRC 或零未连接门槛，不能制造。

### 4. nRF52840 wearable motion beacon

执行层面：

- SSE 完整，但在 Architect 阶段停止，流水线为 0/17。
- 最终证据为 `no grounded KiCad symbol; local_library_structured_extraction_failed`。
- 没有对应的 `*generalfix-v6` run 目录，也没有任何 KiCad、路由或制造产物。

主要失败根因：

- v6 无法把带订货/封装后缀的请求器件稳定落到已安装的 KiCad family/base symbol。
- 后续本地符号库结构化提取也失败，因此确定性门禁没有接受仅由叙述声称的符号可用性。

结论：这是 5 个案例中唯一的执行级阻断，验证了 qualified-base 回查缺口，而不是 PCB 流水线本身的执行情况。

### 5. STM32F072 USB MIDI control surface

执行层面：

- SSE 完整，17/17 步完成。
- Freerouting 在 pin-map/footprint 校验时失败：`matched 259/261 logical pins (278 physical pads assigned)`。
- 实际没有 DSN 或 SES。
- ERC 29 errors，DRC 3 errors；DRC `unconnected=0` 不能代表路由完成。

主要失败根因：

- 连接器终端数量/封装角色语义不匹配。
- LED 限流、SWD、reset/boot 与若干逻辑引脚连接不完整。
- pin-map/footprint mismatch 阻止路由阶段产生 DSN/SES。

结论：保留了原理图、PCB、BOM、CPL 和制造中间文件，但没有实际 Freerouting 产物，不能制造。

## v6 之后的通用修复状态

以下项目不在本次 v6 容器中，不能用本报告的 v6 结果宣称它们已通过 E2E 验证：

| 通用修复 | 当前状态 | 目标 |
|---|---|---|
| qualified-base 回查 | 已完成，尚未进入 v6 容器 | 对带订货/封装后缀的器件先进行受控 family/base 精确回查，同时保留请求 MPN 与真实库符号身份边界 |
| route 最大尝试 3 次、单次 timeout 600 秒 | 已完成，尚未进入 v6 容器 | 给路由组合策略设置共享预算，避免一个案例因多 seed/多 profile 无限占用时间 |
| route metric same-grain | 已完成，尚未进入 v6 容器 | 避免把 net、connection、DRC item 和失败前 PCB 的统计混为同一成功指标 |
| semantic footprint 修复 | 进行中 | 根据器件角色、真实 pin-pad 语义和封装家族选择兼容 footprint，而不是仅按文本名匹配 |
| component library closure | 进行中 | 对 BOM 中所有关键器件形成真实 symbol、footprint、pin map 和嵌入库的闭包，避免用显示值冒充器件 |

这些修复需要进入同一镜像后，使用 targeted regression 和新的盲测矩阵重新验证。

## 覆盖范围限制

本矩阵覆盖 RP2040、STM32G431、ESP32-C3、nRF52840、STM32F072，以及数据记录、电机控制、隔离通信、可穿戴和 USB MIDI 等不同需求，但仍然偏向 MCU 控制板。

它没有覆盖 FPGA、高速 DDR/PCIe、纯模拟/射频、电源模块、背板、柔性板、刚挠结合、封装/模块设计等 KiCad 领域。因此本结果不能外推为“各种 KiCad 项目都已被覆盖”。

此外，这 5 个提示词是 Harness-aware 的确定性回归输入，显式包含 agent 链、17 步、AHE/EHE、artifact-first 或状态门禁等约束。它们适合验证 Harness 回归，却不能替代自然语言不规范输入、越界请求和意图歧义的盲测。

## 后续 targeted regression

> 本节为后续结果占位，不包含尚未执行的成功声明。

### A. nRF52840 qualified-base 回归

- [ ] 使用原始 nRF52840 案例重跑。
- [ ] 记录 requested MPN、回查 query、最终 grounded symbol 和 footprint。
- [ ] 验证不会把不相关 base candidate 当作真实器件。
- [ ] 验证至少进入 selection，并记录最终 17 步、ERC/DRC、DSN/SES 与 release 状态。

### B. pin-map/footprint 回归

- [ ] 重跑 STM32G431 与 STM32F072。
- [ ] 验证 semantic footprint 与 component library closure。
- [ ] 验证 DSN/SES 是否真实生成。
- [ ] 对照 routed nets、unrouted connections、DRC unconnected 的同粒度指标。

### C. 路由预算回归

- [ ] 验证所有 profile/seed 共享最多 3 次调用预算。
- [ ] 验证单次超时为 600 秒。
- [ ] 验证预算耗尽后保留最佳 PCB、DSN、SES 和证据，不误报成功。

### D. 新盲测矩阵

- [ ] 至少加入一个非 MCU 项目。
- [ ] 至少加入一个自然语言不规范但可澄清的 build 请求。
- [ ] 至少加入一个纯 review 请求，验证不会误进入 build。
- [ ] 分别报告 SSE、执行完成、路由、ERC/DRC 和 release 五层结果。

## 可重复核对方法

1. 从 `stream_summary.json` 核对每个案例的 `duration_seconds`、`wall_duration_seconds`、`sse_completed` 和最终消息类型。
2. 从对应 `pipeline_result.json` 核对 `completed_steps`、`execution_complete`、`routing`、`verification`、`release_blockers` 和 `release_ready`。
3. 对 run 目录递归枚举文件，并只统计 `Length > 0` 的 `.kicad_sch`、`.kicad_pcb`、`.dsn`、`.ses`、ERC/DRC JSON、BOM、CPL 和 Gerber/钻孔文件。
4. 对没有 `pipeline_result.json` 的案例，回到 `final_message.json` 核对执行停止阶段，不用叙述性输出替代确定性门禁。
