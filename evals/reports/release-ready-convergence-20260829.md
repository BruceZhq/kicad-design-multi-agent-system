# Release-ready 收敛与局部配对评测报告（2026-08-29）

## 结论先行

本轮在不启动第三个案例的前提下，完成了两个受支持黄金板型的严格发布：NE555 LED 闪烁板和 STM32F030 最小开发板均达到 `release_ready`。两者均为 17/17 步、真实 Freerouting 完成、ERC errors=0、DRC errors=0、DRC unconnected=0，并由控制面登记可信 Manifest。

当前可用的完整单 Agent/多 Agent 配对只有 NE555 一组：单 Agent 完成 17 步但为 `delivered_with_issues`，多 Agent 最终为 `release_ready`。因此在这 **1 个完整 pair** 上，严格任务成功率和 release-ready 率均由 0% 提升到 100%。样本量不足以外推总体收益，不能写成稳定生产成功率。

机器可读证据见 [release-ready-convergence-20260829.json](release-ready-convergence-20260829.json)。

## 严格成功口径

只有同时满足以下条件才计为成功：

1. Hardware Engineer 完成 17/17 步。
2. Control Plane 终态为 `release_ready`，而不是 `delivered_with_issues` 或 `execution_blocked`。
3. `kicad-cli` ERC errors=0。
4. `kicad-cli` DRC errors=0、unconnected=0。
5. Freerouting 的网络与连接全部闭合。
6. 可编辑工程和制造产物进入内容寻址 Manifest。

警告不会被字符串过滤后冒充通过。当前 KiCad library mismatch 只在板内实例和已安装库的规范化制造结构、pad/net 分配及摘要完全一致时，由 digest-bound warning contract 判定为 `auto_equivalent`。

## 多 Agent 严格发布样本

| 案例 | 终态 | ERC | DRC | 未连接 | 路由 | 发布产物 | Manifest |
|---|---|---:|---:|---:|---:|---:|---|
| NE555 LED 闪烁板 | `release_ready` | 0 | 0 | 0 | 6/6 nets，15/15 connections | 55 | `48bb4619…` |
| STM32F030 最小开发板 | `release_ready` | 0 | 0 | 0 | 16/16 nets，50/50 connections | 66 | `afa01682…` |

在这两个已执行的多 Agent 受支持样本上，release-ready 结果为 2/2（100%）。这是固定样本的观察值，不是对任意 PCB 需求的成功率承诺。

## 完整配对结果

| 指标 | 单 Agent | 多 Agent | 多 Agent - 单 Agent |
|---|---:|---:|---:|
| 样本数 | 1 | 1 | — |
| 完成 17 步 | 100% | 100% | 0 pp |
| 严格任务成功率 | 0% | 100% | +100 pp |
| release-ready 率 | 0% | 100% | +100 pp |
| ERC errors | 2 | 0 | -2 |
| DRC errors | 12 | 0 | -12 |
| 活跃运行时间 | 988.801 s | 4877.879 s | +3889.078 s |

耗时不构成多 Agent 更快的证据：多 Agent 结果跨 7 个工程恢复 revision 收敛，包含 Harness 修复期间的重复执行；其价值是最终闭合并保留审计链，而不是本轮吞吐更优。

## STM32F030 根因与通用修复

### 1. 错误的功能所有权

`C5` 的角色为 `ldo_output_capacitor`，电气连接和语义均指向 `U2 (ldo_regulator)`。旧 real-pad 距离函数却从当前物理位置选择最近的可用 IC，错误选中 `U1 (MCU)`。修复后，锚点先由角色语义、已验证连接图和候选类型唯一确定，再计算对应真实 pad 的旋转后坐标和距离。

### 2. 依赖逆序与软目标失效

只修锚点仍不足以发布。通用 MaxRects 打包器按 footprint 尺寸排序，大型从属电容可能先于它服务的 IC 落位；第二遍即使刷新目标坐标，也会重复逆序。系统现在构造 `dependent -> functional anchor` 依赖图，并保证锚点先于从属件参与打包。

### 3. “全部塞下”不等于布局正确

旧的 dependency-aware courtyard shape packer 只在存在未放置器件时触发。当前案例 24 个器件都能放入 45×35 mm 板框，但 C5 在 U2 周围 15 mm 内没有单器件移动的合法位置，局部搜索因此必然停滞。现在只要任一严格布局门禁失败，就会触发功能簇联合重排；候选按“未放置数、严格错误数、目标误差”排序，而不是只看面积能否装下。

真实检查点回放中，C5→U2 pad 距离从 17.13 mm 降到 5.89 mm，布局严格错误由 1 降到 0，没有修改 15 mm 门禁，也没有更改原始需求。

### 4. 路由阶段的 Agentic Recovery

新布局首次路由停在 49/50 connections。连续局部重放产生相同 board hash 和相同分数后，Harness 阻止原样重试。AHE 依次验证局部修复和 `route_planes` 假设，随后把回滚点调整到 `route_plan`；新路由结果达到 50/50、DRC unconnected=0。最终 revision 中记录 5 个 recovery turn，其中 1 个由确定性门禁验证为成功。

## 检查点续跑证据

最终 revision 的首个 Hardware Engineer 事件为 `layout_general` 第 11/17 步，启动时 `completed_steps=10`。Architect、Parts Specialist、原理图、ERC 和前 10 个 EDA 步骤没有重跑。后续 AHE 只在当前失败步骤及允许的最窄上游范围内回滚。

## 不能从本报告得出的结论

- STM32F030 没有已完成的单 Agent 对照，不能放入配对差值。
- 没有盲审记录，人工验收通过率为 N/A。
- 历史事件已经压缩，无法为跨角色交接错误率提供完整分母，该指标为 N/A。
- 单 Agent 历史结果没有结构化工具调用样本，无法公平计算两臂工具参数 schema 正确率。
- 两个 release-ready 正向样本不能代表高速、射频、复杂电源或任意自由需求。

后续只有在同一冻结配置下补齐更多完整 pair、人工盲审和完整工具事件证据后，才适合形成简历中的稳定统计结论。
