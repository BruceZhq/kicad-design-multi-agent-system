# SAME54 工业边缘网关端到端构建案例

## 案例用途

本案例用于验证 RatsNestPro 是否能够把包含“Reviewer、审查、ERC、DRC”等后置验收要求的长文本，正确识别为 `build`，并执行完整的多智能体硬件设计流程。

案例元数据：

- `project_name`: `same54-industrial-edge-gateway`
- `run_name`: `same54-industrial-edge-gateway-e2e-v2`
- `primary_intent`: `build`
- `llm_mode`: `required`
- 固定主 MCU：`ATSAME54P20A-AU`
- 目标产物：KiCad 原理图、PCB、DSN、SES、BOM、CPL、Gerber 和独立审查报告

预期工作流：

```text
Supervisor
→ Architect
→ Parts Specialist
→ Hardware Engineer
→ Reviewer
→ 必要时 Hardware Engineer 修复
→ Reviewer 复审
→ Supervisor 汇总
```

> 注意：运行状态中由系统追加的 `GROUNDED ARCHITECT EVIDENCE` 不属于用户提示词。本案例不包含该运行时数据，避免把错误的器件解析结果固化到测试输入中。

## 可直接使用的提示词

```text
This is a new PCB design-and-build request. Route this request to build mode.
There is no existing KiCad project to review.

请从需求开始设计一块新的工业以太网与 CAN-FD 数据采集网关。

project_name:
same54-industrial-edge-gateway

run_name:
same54-industrial-edge-gateway-e2e-v2

llm_mode:
required

必须执行完整的新建 PCB 流程，不得把本任务识别为“审查已有工程”。

禁止调用、复制、重命名或回退到 ATmega328P、STM32F405、RP2040 等已有案例或离线模板。

本题只固定主 MCU。其他辅助器件必须由 Architect、Parts Specialist 和 Hardware Engineer 根据官方资料、真实 KiCad 库、接口要求、供电能力和可制造性自主选择。

一、多智能体工作流

必须执行：

Supervisor
→ Architect
→ Parts Specialist
→ Hardware Engineer
→ Reviewer
→ 必要时 Hardware Engineer 修复
→ Reviewer 复审
→ Supervisor 汇总

具体要求：

1. Architect 必须进行实际资料检索，覆盖：

   - ATSAME54P20A-AU 官方数据手册；
   - SAME54 官方硬件设计、时钟、电源和 Ethernet 资料；
   - 所选 RMII Ethernet PHY 的数据手册和参考设计；
   - 所选 CAN-FD 收发器的数据手册；
   - 所选 9–36 V 降压芯片的数据手册；
   - USB Type-C Device/Sink 的 CC 电阻要求；
   - 所选 QSPI Flash、传感器和保护器件资料。

2. Architect 应给出设计依据和关键计算，但不得因为辅助器件尚未最终选定而提前终止。辅助器件选型应继续交给 Parts Specialist 和 Hardware Engineer。

3. Parts Specialist 应验证真实 MPN 和本地采购信息。本地目录不可用时允许返回 unavailable，并继续工程设计，但必须标记“采购状态未验证”。禁止编造 LCSC 编号、价格或库存。

4. Hardware Engineer 必须执行完整的原理图、PCB、Freerouting 和制造文件流程，不得只输出设计说明。

5. Reviewer 必须审查本次 Hardware Engineer 新生成的实际工程。存在可修复的确定性错误时，应把结构化反馈交回 Hardware Engineer。

二、主控制器

主 MCU 固定为：

ATSAME54P20A-AU

要求：

- 使用真实 KiCad 符号；
- 使用兼容的 TQFP-128 封装；
- 不得替换为其他 SAME、SAM、STM32、ESP32、RP2040 或 ATmega；
- 所有数字、模拟和核心电源引脚按照官方要求连接；
- 每组电源引脚具备合理去耦；
- 包含官方要求的核心稳压电容网络；
- 包含主晶振和 32.768 kHz RTC 晶振；
- 包含复位电路、启动配置和标准 Cortex SWD 接口；
- 不允许悬空关键电源、地、复位、时钟或启动引脚。

晶振频率、负载电容和核心电容值应来自官方设计依据。

三、电源系统

输入包括：

A. 9–36 V 工业直流输入；
B. USB-C VBUS 5 V。

要求：

- 工业输入为主要电源；
- USB-C 可在工业电源不存在时为 MCU、调试接口和低功耗外围供电；
- 两路输入之间不得反向灌电；
- USB-C 不得反向给工业输入供电；
- 工业输入具有保险、反接保护、TVS、滤波；
- 9–36 V 转换为 5 V；
- 5 V 转换为 3.3 V；
- 其他必要电压轨由设计自主确定；
- 电源芯片必须满足输入范围、输出电流、热耗散和稳定性要求；
- 输入输出电容必须符合对应数据手册；
- 给出各电源轨功耗预算；
- 说明 USB 供电模式下被限制的高功耗功能。

四、USB-C

USB-C 工作在 USB 2.0 Full-Speed Device 模式。

要求：

- CC1、CC2 分别配置正确的 Rd；
- D+、D− 具有合适串联电阻；
- 使用真实的双通道 USB ESD 器件，或者两颗独立单通道器件；
- VBUS 具有输入保护；
- 说明 Shield 接地策略；
- D+、D− 不得与电源或 GND 短接；
- PCB 上按照 USB 差分对处理；
- 无法精确验证阻抗时，标记为“需要板厂叠层复核”。

五、Ethernet

使用 SAME54 的 RMII Ethernet MAC，并自主选择真实 RMII PHY。

要求：

- PHY 具有真实 KiCad 符号和兼容封装；
- 使用带隔离变压器的 RJ45，或者外部磁性器件加 RJ45；
- 正确连接 RMII TXD、RXD、TXEN、CRS_DV、MDC、MDIO 和参考时钟；
- PHY 地址配置不得悬空；
- MDIO 具有正确上拉；
- REF_CLK 方向和频率符合 PHY 工作模式；
- PHY 到磁性器件的差分线按照 100 Ω 差分对处理；
- 网口侧具有 ESD 和浪涌防护；
- PHY 模拟电源、去耦和中心抽头按照数据手册连接；
- PHY、磁性器件和 RJ45 形成紧凑区域。

六、CAN-FD

提供一路 CAN-FD 接口。

要求：

- 使用真实 CAN-FD 收发器；
- 与 3.3 V MCU IO 兼容；
- 主体使用 5 V 时必须正确处理 VIO；
- CANH/CANL 具有 TVS 和共模抗扰措施；
- 120 Ω 终端电阻通过跳线选择；
- 接口提供 CANH、CANL 和 GND；
- CANH/CANL 按差分对处理；
- 不得把普通 CAN 2.0 收发器冒充 CAN-FD 收发器。

七、存储系统

1. microSD：

- 使用 SDHC/SDIO 4-bit 模式；
- 正确连接 CLK、CMD、DAT0–DAT3；
- 包含必要上拉、去耦和 ESD；
- SD 时钟与晶振、RMII 和模拟输入保持隔离。

2. QSPI NOR Flash：

- 容量至少 128 Mbit；
- 使用真实 KiCad 符号和兼容封装；
- 正确连接 CS、SCK、IO0–IO3；
- 不得与 SDHC、RMII、CAN-FD 或 SWD 产生 MCU 引脚冲突；
- Flash 靠近 MCU，并具有独立去耦。

八、传感器与扩展

- 增加一颗数字温湿度传感器；
- 具体型号由智能体自主选择；
- 优先使用 I²C；
- I²C 上拉阻值合理；
- 提供外部 I²C 扩展接口，包含 3.3 V、GND、SCL、SDA；
- 外部接口具有基础 ESD 防护；
- 检查内部传感器和外部扩展的地址及总线上拉负载。

九、模拟输入

提供两路 0–10 V 工业模拟输入。

每路包含：

- 输入限流；
- 电阻分压；
- RC 低通滤波；
- ADC 过压保护；
- 输入连接器；
- 合理的模拟接地和回流路径。

必须给出：

- 分压电阻值；
- 10 V 输入时的 ADC 电压；
- 考虑电阻容差后的最大 ADC 电压；
- RC 截止频率；
- 输入阻抗；
- ADC 采样时间依据。

在全部允许输入条件下，ADC 引脚不得超过绝对最大额定值。

十、人机接口

包括：

- 工业输入电源状态 LED；
- 3.3 V 电源状态 LED；
- 系统状态 LED；
- Ethernet Link/Activity 指示；
- 用户 LED；
- 用户按键；
- 复位按键。

所有 LED 必须有限流电阻，所有按键输入不得悬空。

十一、PCB要求

采用四层板：

- L1：元件、高速信号和关键信号；
- L2：连续 GND 平面；
- L3：电源和低速信号；
- L4：普通信号。

板框不大于：

90 mm × 65 mm

布局要求：

- 工业电源、USB、Ethernet、CAN 和模拟输入连接器靠近板边；
- TVS/ESD 靠近对应连接器；
- 开关电源远离 PHY、晶振、ADC 和传感器；
- MCU 去耦靠近对应电源引脚；
- 晶振和负载电容靠近 MCU；
- QSPI Flash 靠近 MCU；
- PHY、磁性器件和 RJ45 形成紧凑区域；
- Ethernet、USB 和 CAN 差分对不得跨越参考平面分割；
- 模拟输入区域与开关电源、RMII 和 CAN 区域隔离；
- 提供四个安装孔；
- 不允许元件重叠、越界或符号封装不兼容。

制造能力应从系统配置读取。用户未明确指定某项布线几何值时，可以在板厂制造能力范围内自适应选择，但不得降低电气验证标准。

十二、成功验收条件

成功至少要求：

- 主 MCU 确实为 ATSAME54P20A-AU；
- MCU 使用真实符号和兼容 TQFP-128 封装；
- 不包含其他 MCU 替代品；
- 所有选择器件使用真实符号和兼容封装；
- 不允许仅修改显示值冒充其他器件；
- 符号引脚号与封装焊盘号兼容；
- 不存在一个引脚属于两个不同网络；
- 不存在未解析逻辑引脚；
- 电源、时钟、复位、启动和核心电容网络完整；
- USB、RMII、CAN-FD、SDHC、QSPI 和 SWD 不存在 MCU 引脚冲突；
- 产生实际 KiCad 原理图文件；
- 产生实际 KiCad PCB 文件；
- KiCad ERC error 为 0；
- KiCad DRC error 为 0；
- Freerouting 真实执行；
- 产生 DSN 和 SES；
- SES 成功导回 PCB；
- unconnected 为 0；
- 输出 BOM、CPL 和 Gerber；
- Reviewer 对本次新生成的工程完成独立审查。

Warning 不自动等同于失败，但必须分类列出：

- 电气风险；
- 布局和丝印问题；
- 阻抗或叠层待板厂复核事项；
- 采购状态未验证；
- 制造前人工检查事项。

如果出现确定性错误：

- 先进行有限次数的针对性修复；
- 修复不得删除用户要求的功能；
- 不得替换固定 MCU；
- 不得降低 ERC/DRC 等级；
- 不得把 warning、blocked 或 not_reached 描述为成功；
- 达到修复轮次上限后仍未通过时返回 blocked，并保留全部中间产物。
```

## 关键断言

该案例至少应验证：

1. 意图路由结果为 `build`，不能因为文本包含 Reviewer 和审查要求而进入 `review`。
2. Architect 对固定 MCU 的解析结果必须是 `ATSAME54P20A-AU`，不得被候选器件、网页片段或运行时追加文本污染。
3. 本地采购目录不可用只能降低采购证据状态，不能让硬件工程提前停止。
4. 缺失支持器件时应触发 AHE 的结构化补全，而不是立刻结束整个流程。
5. Reviewer 只能审查本次流程实际生成的工程路径。
6. 成功状态必须同时满足真实产物、ERC、DRC、Freerouting、连接性和独立审查门禁。

## 与架构文档的关系

本案例可作为以下架构方案的端到端回归输入：

- 结构化意图识别；
- AHE 任务内诊断和修复循环；
- EHE 跨任务能力缺口归纳；
- 状态版本、Checkpoint 和独立 Reviewer 门禁。

详见 [`Intent_Routing_and_AHE_EHE.md`](../Intent_Routing_and_AHE_EHE.md)。
