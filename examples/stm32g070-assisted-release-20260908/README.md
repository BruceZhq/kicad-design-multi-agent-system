# STM32G070 环境监测主控板：真实发布工程

这是 2026-09-08 完成发布的工程副本，不是新强模型执行器自主成功率的评测结果。原任务经过多智能体流程、Codex 辅助实体修复、正常检查点续跑、制造重建和 Reviewer 审查后达到 release_ready。

打开 `stm32g070rbt6-board.kicad_pro`；原理图与 PCB 可直接编辑。项目局部 Symbol/Footprint 已实体化复制到 `.ratsnest-libs`，通过 `${KIPRJMOD}` 引用，不依赖原机器绝对路径。使用 KiCad 9 或兼容版本；3D 模型可选，未复制整个系统 3D 库。

## 可核查结果

| 指标 | 结果 |
|---|---|
| 工程步骤 | 17/17 |
| ERC errors | 0 |
| DRC errors / warnings / unconnected | 0 / 0 / 0 |
| 网络 / 连接闭合 | 18/18；86/86 |
| 发布状态 | release_ready |
| 本包导出的最终工程文件 | 54 |

ERC 仍有 1 条 `lib_symbol_mismatch` warning；原审查以嵌入符号与已安装库的结构等价证据处理，未删除或抑制报告。证据保留在 [release-evidence.json](release-evidence.json)。采购 BOM 仍如实记录供应商证据不足，不能把电气发布就绪当成采购库存保证或样板实物验证。

[原始需求及确认参数](requirement.md)。`gerber/` 内包含制造层和钻孔。Production BOM 与 Procurement BOM 分别保留 EDA 资产信息和采购证据状态；CPL 为贴片坐标。

最终 PCB 的原始 SHA-256 为：

`74bb89ecdbf14f626e90eb1d44583bc390c88364b817442eacc511f7294aacf1`

CAD 和制造文件按原字节复制。符号库只摘出实际引用的定义及继承父符号，避免复制十几 MB 无关库；定义内容不变，证据中分别记录来源库摘要和导出子集摘要。证据 JSON 将原运行目录改为相对路径，各文件摘要单独列出；Git 禁止对本例自动转换换行，确保克隆后的摘要可核查。控制面的原 64 件产物与本包 54 件工程文件口径不同：本包不包含会话、内部检查点、签名凭据、原始模型日志、厂商 PDF，以及早于人工修复的 DSN/SES 快照。最终铜线以 PCB 与对应最终 DRC 为准。

KiCad 社区库的版权和许可见 KICAD-SYMBOLS-COPYRIGHT.txt、KICAD-FOOTPRINTS-COPYRIGHT.txt；局部生成符号不是原厂认证库，其工程核验状态以原发布证据为准。制造前仍应完成具体板厂规则复核和人工工程验收。
