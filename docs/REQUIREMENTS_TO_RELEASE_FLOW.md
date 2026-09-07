# 从需求到发布：多智能体与隔离强模型执行器

图中橙色为本次新增/增强；蓝色为已有链路。17 步外层顺序不变，候选编辑与反思在所属工程步骤内部完成。没有模型拥有修改发布判定的权限，也不能承诺任意需求都成功。

```mermaid
flowchart TD
    U["用户输入需求；选择主模型、视觉模型"] --> UI["新增：独立选择升级模型与推理强度"]
    UI --> API["Next.js BFF → Java 控制面<br/>身份、租户、不可变 Run 配置"]
    API --> LG["Python / LangGraph Supervisor"]
    LG --> Q{"需求是否存在必须用户决定的歧义？"}
    Q -- 是 --> HITL["Decision Engine → AG-UI 人工问答"]
    HITL --> LG
    Q -- 否 --> AP["Architect + Parts + 可选 Specialist<br/>本地资产、资料、RAG、官方证据"]
    AP --> CLOSE["选型后锁定 Footprint<br/>Symbol / MPN / Pin-Pad / 资产闭包"]
    CLOSE --> T["Temporal：原 Run 与检查点<br/>1–5 需求、拓扑、选型、连接、PinMap"]
    T --> SCH["6–8 原理图布局、实体生成、ERC"]
    SCH --> L["9–12 分区、关键/普通布局、实体 PCB"]
    LIB["增强：经 Reviewer 晋级的电路模块<br/>相对布局与内部走线种子"] --> L
    L --> R["13–15 规则、电源平面、信号布线"]
    R --> PRE["新增：路由前几何拥塞观察<br/>相邻引脚成组、候选出口"]
    PRE --> ROUTE["真实 Freerouting / KiCad 工具"]
    ROUTE --> CHECK{"当前实体检查通过？"}
    CHECK -- 是 --> FAB["16–17 丝印/制造输出重建<br/>ERC、DRC、连接性、需求不变量"]
    CHECK -- 否 --> OWNER["结构化失败证据 → 故障所有者"]
    SCH -- ERC 失败 --> OWNER
    FAB -- 校验失败 --> OWNER
    OWNER --> INFRA{"基础设施 / Provider 故障？"}
    INFRA -- 是 --> RETRY["原检查点有限重试<br/>不重新选型、不把断网当设计错误"]
    RETRY --> T
    INFRA -- 否 --> AHE["AHE：观察、计划、真实 CAD 动作<br/>验证、反思、候选回滚"]
    AHE -- 已改善 --> CHECK
    AHE -- 布线连续无改善且已启用升级 --> STRONG
    subgraph STRONG["新增：强模型隔离编程修复会话"]
      OBS["真实 CAD 渲染 + 焊盘/走线/障碍 + KiCad 报告"]
      PLAN["强模型规划组合动作<br/>有界联合扇出搜索辅助"]
      EXEC["无网络、无业务密钥、只读根目录<br/>限额工作副本运行 Python + pcbnew"]
      VERIFY["宿主重新填铜、DRC、功能布局与需求核验<br/>冻结器件/引脚/板框/规则身份"]
      BEST{"相对最佳候选确有改善？"}
      OBS --> PLAN --> EXEC --> VERIFY --> BEST
      BEST -- 否 --> REFLECT["反馈失败证据；改变策略或恢复最佳副本"]
      REFLECT --> OBS
    end
    BEST -- 是 --> COMMIT["新增：文件指纹 CAS + 原子替换<br/>同步布局状态、写提交恢复日志"]
    COMMIT --> CHECK
    AHE -- 非路由问题 --> OWNED["沿现有所有权修复对应工程步骤"]
    OWNED --> T
    REFLECT -- 预算耗尽或硬冲突 --> HUMAN["保留证据和检查点；说明阻碍或请求授权"]
    FAB -- 全部通过 --> REV["独立 Reviewer 复核最终实体与发布身份"]
    REV -- 未通过 --> OWNER
    REV -- 通过 --> RELEASE["release_ready<br/>可编辑工程、Gerber、钻孔、BOM、CPL、Manifest"]
    RELEASE --> LIB
    OWNER -. 跨项目重复 Harness 缺陷 .-> EV["既有 Governed Evolution<br/>隔离评测 → 人工审批 → 版本化发布"]
    classDef default fill:#eef4ff,stroke:#517fb5,color:#172d49;
    classDef added fill:#fff0db,stroke:#e28a16,color:#38240c;
    classDef released fill:#daf3e3,stroke:#21834a,color:#123a23;
    class UI,LIB,PRE,OBS,PLAN,EXEC,VERIFY,BEST,REFLECT,COMMIT added;
    class RELEASE released;
```

## 哪些恢复会重跑？

图中回到 Temporal 表示恢复耐久调度入口，不表示从第 1 步重做。前缀检查点仍由既有恢复契约验证并跳过；布局/路由修复只失效受影响的下游制造产物。强模型的提交日志用于处理“PCB 已替换、进程尚未来得及持久化布局状态”的崩溃窗口。

## 新增能力的实际边界

- 路由前拥塞观察提供几何证据，不是新增的刻板阻断门禁。
- 联合搜索为至多 8 个相关引脚同时预留线段和过孔；它可能找不到解，也不代替完整布线。模型可改布局、拆线、换策略，再由真实工具验证。
- 强模型可以编写项目副本内的 Python CAD 修复程序，不能直接改生产生成器、原理图电气身份、发布门禁或用户硬约束。生产代码改进仍走既有受治理发布路径。
- 模块模板需要相同的已验证器件资产，并重查新任务的空间与电气约束；历史成功不是当前任务自动放行凭证。
- 原实例已成功；新执行器的隔离和真实 CAD 读写经过检查，不据此宣称已完成新模型自主端到端成功率评测。
