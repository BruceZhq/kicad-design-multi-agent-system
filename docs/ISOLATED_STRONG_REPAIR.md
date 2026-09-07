# 独立强模型 CAD 修复执行器

这一层不是聊天兜底，也不替代原 17 步工作流。它在路由修复持续无改善时接手一个工程副本：观察实际几何和 CAD 图片，编写 Python 修改该副本，由宿主重新运行真实 KiCad 校验，改善后才提交回原工作区。

## 模块边界

| 模块 | 职责 | 不拥有的权限 |
|---|---|---|
| `frontend/components/strong-repair-controls.tsx` | 独立升级模型、推理强度、浏览器偏好 | 不传 API key，不决定预算和门禁 |
| BFF → Java RunRuntimeConfiguration → Python Runtime | 校验型号，持久化到 Run，并传递 Temporal | 不允许恢复时偷偷换模型配置 |
| `ratsnestpro/repair/contracts.py` | 请求、候选、服务端额度契约 | 不运行脚本 |
| `ratsnestpro/repair/session.py` | 观察—执行—反馈；最佳候选、重复动作抑制 | 不直接写正式 PCB，不判定 release_ready |
| `ratsnestpro/repair/routability.py` | 路由前几何观察、相关引脚分组 | 不因软启发式拒绝交付 |
| `ratsnestpro/repair/joint_escape.py` | 有界联合出口/过孔预留；副本执行辅助 | 不豁免真实 DRC，不自动改引脚功能 |
| `ratsnestpro/repair/pipeline_adapter.py` | 实体查询、渲染、权威检查、CAS 提交与状态同步 | 不覆盖选型、电气身份、板框或设计规则 |
| `repair_executor/` | 私有鉴权 HTTP 服务，创建一次性容器 | 不接收宿主路径、镜像选择、任意容器参数 |
| `ratsnestpro/knowledge/layout_modules.py` | 从发布 PCB 抽取模块相对布局和内部走线种子 | 不自动批准跨任务复用 |

已有电路模块库仍负责内容摘要与 Reviewer 晋级。增强模块在同一租户/Harness 分区检索；生成候选和独立审查都会重新绑定原 PCB 摘要。布局模板进入布局/路由规划上下文，由模型适配当前任务，随后仍检查资产、功能布局及最终 DRC，不能直接复制后宣布成功。

## 执行与提交

1. 前端选择的升级模型与主模型、视觉模型独立。只列出 Runtime 已配置的候选型号；配置存在不等于供应商账户一定具备访问权限。模型调用使用原提供方密钥，服务端直接采用选定型号，不再被 purpose 路由偷偷换成小模型。
2. 普通路由修复进入第二次失败处理时可升级；每 Run 默认最多 2 个设计修复会话，每会话最多 10 轮。独立 Token 台账累计上限 60,000，脚本每次最多 90 秒（契约硬上限 120 秒）；新轮次窗口 600 秒，模型调用按剩余时间约束。最终工具校验仍必须结束并给出可信结果。
3. 模型得到当前 PCB、精确焊盘/铜线/障碍查询、两面真实渲染，以及当前 KiCad 报告。它返回观察请求或 Python 程序，不需要供应商原生工具调用或 JSON Schema 模式。
4. Broker 只接收白名单工程文件。脚本容器无网络、无业务密钥、无 Docker socket、无宿主 bind mount；UID 10001、只读根目录、256 MiB 临时工作卷、128 MiB `/tmp`、2 GiB 内存、1 CPU、128 进程上限。输入通过只执行固定等待命令的可信容器交付，模型只在另一只读容器中运行。执行结束只取回 PCB，所有候选容器与临时卷按精确 ID 清理。
5. 宿主使用原项目规则与约束重新填铜和检查 DRC、连通性、功能布局与需求不变量。器件身份、封装、焊盘、网络绑定、板框、层叠和规则内容由独立摘要冻结；模型在副本修改的报告或规则文件不作为判定依据。
6. 允许在副本内分多轮做联合修改，但只有相对最佳候选没有指标退步且有实际改善的结果才入选；违反冻结约束则回滚。相同 PCB 摘要上的相同程序不得原样重复。
7. 提交先核对原 PCB 指纹，再写恢复日志、原子替换 PCB，并同步布局 Artifact。原候选回滚排除 `.strong-repair`，不能倒退预算和审计日志。恢复时只在新 PCB 摘要匹配时协调布局状态，不覆盖另一个已经变化的设计。
8. 返回原路由检查，重建后续制造产物，最后经独立 Reviewer 和 Manifest 才能发布。Broker/Provider 失败交还既有 Temporal/Provider 恢复策略，不据此重新生成电路；基础设施重试与设计会话预算分开记录。

## 本地启用

本机已生成被 Git 忽略的 `.env.strong-repair`，内含独立执行器 Token 和固定本地 Sandbox image ID。不要提交或截图其中的值。

其他用户应自行建立此文件：设置 `RATSNEST_REPAIR_EXECUTOR_TOKEN` 为至少 32 字符的加密随机值；设置 `RATSNEST_REPAIR_SANDBOX_IMAGE` 为管理员审核过的镜像 digest 或本地 image ID。普通 `compose.yaml` 默认不启动编程执行器。

在仓库根目录执行：

```powershell
docker compose build agent_service frontend control_plane
docker image inspect ratsnestpro-agent-runtime:local --format '{{.Id}}'
# 新构建后，将上面 ID 更新到本地 .env.strong-repair 的 Sandbox image 配置。
docker compose --env-file .env --env-file .env.strong-repair -f compose.yaml -f deploy/compose/strong-repair.yaml --profile identity --profile control-plane --profile artifact-store up -d
```

访问 `http://localhost:8088/`，在新任务中选择“强模型升级执行器”和“升级执行推理强度”。当前选择只影响新 Run；旧任务恢复继续采用其保存的配置。关闭该选项时，保留现有多智能体与普通 AHE，不启动额外模型调用。

Broker 属于高权限基础设施：本地方案中只有它持有 Docker daemon socket。**不要将此端口公开，也不要直接将开发 Compose 当作公网多租户沙箱**。公网环境应部署到独立的受控执行节点/专用 daemon，并沿用网络隔离、镜像白名单、资源限额、独立身份和审计；本提交没有新建生产 Kubernetes 强模型执行 Broker。既有 Evolution Job 与这个任务级编程执行器是不同边界。

## 已完成的验证与口径

- 前端 TypeScript 检查和正式镜像构建；Java 21 编译与原镜像构建验证完成。
- 模块库兼容性、候选回滚、重复/无改善处理、目录约束、隔离策略、联合出口预留、旧 Workflow 身份兼容及入口参数传递的定向检查。
- Worker → HTTP Broker：无凭据请求 403；正确凭据 200；隔离容器实际用 pcbnew 读取并保存 PCB。
- 在 STM32 发布工程的副本中删除一段 UART 走线，用**固定脚本规划器**驱动真实修复事务：DRC errors / unconnected / warnings 从 `0 / 1 / 2` 回到 `0 / 0 / 0`，触发 `strong_repair.committed`，原发布 PCB 摘要不变。
- 这证明执行与验证链路有效，不代表某个升级模型已经自主完成该案例。本次没有额外付费调用模型重跑黄金任务，更没有承诺未来任意板型 100% release-ready。

查看 [完整流程图](REQUIREMENTS_TO_RELEASE_FLOW.md) 和 [真实 STM32 发布工程](../examples/stm32g070-assisted-release-20260908/README.md)。
