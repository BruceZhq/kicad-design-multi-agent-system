# 外部工程 Agent 修复与终止交付

## 定位

主模型负责多智能体与 17 步草案工作流；外部 A2A Terra 接手已有工程，通过观察、官方资料检索、Python 编程、执行、验证与反思进行修复。不把模型输出的“完成”作为发布证据。具体方法和流程图见 [修复方法](EXTERNAL_REPAIR_METHOD.md)。

## 模块与责任

| 模块 | 责任 |
|---|---|
| `src/ratsnestpro/repair/session.py` | 通用修复指引、查询/编程循环、失败程序记忆、最佳候选、预算终止 |
| `src/ratsnestpro/repair/research.py` | 官方文档搜索、受控 HTTPS 读取、分页、缓存和来源摘要 |
| `src/ratsnestpro/repair/project_host.py` | 实体候选、Python 沙箱调用、原理图/PCB 联合验证与版本化提交 |
| `src/repair_executor/a2a_agent.py` | 独立服务会话、完整输入、终态结果及交付包 |
| `src/ratsnestpro/repair/delivery.py` | 最佳保留工程打包、剩余错误报告、安全落盘；不具有发布授权 |
| `src/ratsnestpro/repair/a2a_client.py` | 校验 A2A 来源身份；先接收交付文件，再决定是否提交改善 |
| `src/ratsnestpro/repair/continuation.py` | 追加授权或结束决定的持久化回执 |
| `src/agents/ratsnestpro/ratsnestpro_agent.py` | HITL 分流；结束修复直接进入最终报告，不重复调用 Hardware |
| `src/agents/ratsnestpro/tools.py` | 将工程 ZIP/错误 JSON 纳入真实 Artifact 清单 |
| `src/agents/ratsnestpro/artifact_publisher.py` | 通过既有存储/授权链路发布；修复 ZIP 不冒充制造包 |

## 交付语义

- `release_ready`：主系统严格检查、制造重建与 Reviewer 全部通过。
- `delivered_with_issues`：用户明确结束，已有文件可下载，但错误保留、不可宣称可制造。
- 最佳候选不是最后一次尝试。无改善时交付原始保留工程；异常程序的未保存变化不作为候选。
- `terra-repair-delivery.zip` 包含已有工程和 `repair-error-report.json`；正常结束的修复轮次还带有 `current-checks/` 中的新验证报告。报告含终止原因、剩余检查、文件 SHA-256 和非发布声明。
- 容器异常/提供方错误的兜底包明确标记验证不可用。调用方不解压远端包到主工程；主工程变更仍经独立验收及版本校验。
- 外部报告不能覆盖主系统的 `release_ready`。安全域、来源版本、用户硬约束不因结束交付而放宽。

## 前端操作

选择主模型、最终修复模型及推理强度并提交需求。若修复仍有问题，确认框显示“批准新增一轮 Terra 修复”和“结束修复，交付当前工程和剩余错误报告”。选择后者，点击确认，当前 Run 结束并在产物区提供工程及报告。不会创建新的设计任务或偷偷追加模型预算。

## 部署

沿用 `compose.yaml` + `deploy/compose/strong-repair.yaml` + `deploy/compose/agent-protocols.yaml` 的 A2A profile。重建 Runtime 后同步更新 `RATSNEST_REPAIR_SANDBOX_IMAGE` 为同一镜像摘要，再重建 agent_service、temporal_worker、repair_executor、external_repair_agent。模型白名单和服务凭据仍由私有环境文件配置，不能提交 Git。

`RATSNEST_REPAIR_DOC_DOMAINS` 控制文档读取官方域名，默认涵盖 KiCad 与常用器件厂商；仅开放文档服务网络，不给 Python 沙箱联网权限。

## 验证口径

本次以非付费针对性测试验证归档、无改善的 A2A 回传、预算终态、HITL 结束、不变更发布真值、路径/摘要防护、研究缓存与私网阻断。没有启动新一轮 Terra 实板评测；不能据此声称独立修复成功率已提高。此前人工辅助通过和 Terra 独立失败仍分别记录。

部署后另以已有失败任务的真实保留工程执行导出验证（零模型调用）：ZIP 为 1,957,249 字节、61 个条目，包含 `.kicad_pcb`、`.kicad_sch`、`.kicad_pro` 和错误报告。此检查验证包的生成与读取，不重新计算或改写历史工程验收结果。
