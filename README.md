# KiCad Design Multi-Agent System

面向真实 KiCad 工具链的多智能体硬件设计系统：用 LangGraph 编排专业 Agent，用 Temporal 耐久执行 17 步 EDA 流水线，并以确定性证据门禁约束交付结论。

[在线 Demo](https://brucezhq.github.io/kicad-design-multi-agent-system/) · [GitHub 源码](https://github.com/BruceZhq/kicad-design-multi-agent-system) · [五案例审计报告](docs/ratsnestpro/test-results/five-case-e2e-general-fixes-2026-07-30.md)

[Agent 可观测与自动评测](docs/observability-and-evaluation.md) · [公开 recorded eval 报告](evals/reports/public-recorded-eval.md)

> Demo 使用模拟数据展示需求提交、Agent 协作、人工确认和产物交付，不调用真实 LLM、KiCad 或任何私有服务。

## 项目一览

### 系统架构图

```mermaid
flowchart LR
    U[用户 / Demo] --> C[Java Spring 控制面\nOIDC · 多租户 · Run · 审计]
    C --> A[Python Agent 内核\nLangGraph Supervisor + Specialists]
    A --> R[知识与工具层\n资料检索 · KiCad 证据 · HITL]
    A --> T[Temporal 耐久执行\nActivity · Heartbeat · Checkpoint]
    T --> K[KiCad / Freerouting\n原理图 · PCB · 路由 · 制造文件]
    K --> V[确定性 Reviewer\nERC · DRC · Manifest · 发布门禁]
    V --> C
    C --> D[(PostgreSQL · Redis · Kafka · S3)]
```

### 17 步 EDA 流程图

```mermaid
flowchart TD
    subgraph S1[需求与器件闭环]
      direction LR
      A1[1 Profile/需求快照] --> A2[2 选型与封装绑定] --> A3[3 符号存在性] --> A4[4 封装存在性] --> A5[5 引脚-焊盘兼容]
    end
    subgraph S2[KiCad 工程生成]
      direction LR
      A6[6 原理图] --> A7[7 网络连接] --> A8[8 板框/层叠/约束] --> A9[9 元件放置]
    end
    subgraph S3[路由与验证]
      direction LR
      A10[10 导出 DSN] --> A11[11 Freerouting] --> A12[12 生成 SES] --> A13[13 导回 PCB] --> A14[14 ERC] --> A15[15 DRC/未连接]
    end
    subgraph S4[交付]
      direction LR
      A16[16 BOM/CPL/Gerber/钻孔] --> A17[17 Reviewer + Manifest]
    end
    A5 --> A6
    A9 --> A10
    A15 --> A16
```

### 五案例 E2E 回归结果

2026-07-30 的 `generalfix-v6` 固定矩阵覆盖 RP2040、STM32G431、ESP32-C3、nRF52840 和 STM32F072 五类板卡需求。结果按证据层级报告：

| 可核查指标 | 结果 | 含义 |
|---|---:|---|
| SSE 请求完整结束 | 5/5 | 长任务事件流未中断 |
| Hardware Engineer 完成 17 步 | 4/5 | 流水线走到末尾，不等于电气正确 |
| 生成非空 `.kicad_sch` 与 `.kicad_pcb` | 4/5 | 保留了可编辑工程草稿 |
| 生成非空 Freerouting `.dsn` 与 `.ses` | 2/5 | 两个案例进入真实路由链路 |
| `release_ready=true` | 0/5 | ERC/DRC 或连通性未通过，系统全部拒绝误放行 |

完整口径、逐案例根因和实际文件统计见[五案例独立审计](docs/ratsnestpro/test-results/five-case-e2e-general-fixes-2026-07-30.md)。`completed_with_issues` 只表示“执行完毕并保留可编辑草稿”，不能解释为可制造。

### 当前能力边界

- 当前回归证明了多智能体编排、长任务恢复、真实 KiCad 产物生成和确定性门禁；尚未证明稳定生成可制造 PCB。
- 五案例主要覆盖 MCU 控制板，不能外推到高速 DDR/PCIe、射频、柔性板或复杂电源设计。
- 静态 Demo 展示交互与证据模型，不执行真实模型推理或 EDA 工具。
- `ratsnestpro`、`ratsnest-*` 是为数据库迁移、内部 API 和工程兼容保留的稳定内部标识，不代表公开产品名称。

### 相比上游模板的主要新增模块

| 维度 | 上游 `agent-service-toolkit` | 本项目新增/重构 |
|---|---|---|
| 任务领域 | 通用 Agent 服务骨架 | 版本化 KiCad Capability Profile 与硬件设计 State |
| Agent 编排 | 通用 Agent 接口 | Supervisor、Architect、Parts Specialist、Hardware Engineer、Reviewer |
| 耐久执行 | 通用请求生命周期 | Temporal 17 步 EDA Workflow、Activity、Heartbeat、Checkpoint 与补偿恢复 |
| 工具与证据 | 通用工具调用 | KiCad/Freerouting 工具链、符号/封装/引脚证据和人工确认 |
| 质量门禁 | 通用模型输出 | 确定性 ERC/DRC、Reviewer、内容寻址 Manifest 与禁止误放行策略 |
| 产品工程 | Agent API | Java 控制面、多租户/RLS、OIDC、SSE 回放、审计、对象存储与 Canary 治理 |

当前生产产品只注册一个 Agent：`ratsnestpro-multi-agent`。旧的通用聊天、独立 RAG/AG-UI、语音和多 Agent 示例代码已经从运行时移除，避免启动时加载无关图和错误地把普通聊天 Agent 当成硬件设计 Agent；正式 AG-UI 事件适配仍属于当前产品链路。

## 当前能力边界

- 支持自然语言硬件需求，包含不完整或非模板化描述。
- 通过五类版本化 Capability Profile 约束任务边界，而不是用固定 BOM 模板回答。
- Supervisor 编排 Architect、Parts Specialist、Hardware Engineer 和 Reviewer。
- Architect 负责意图澄清、官方资料检索、KiCad 符号/封装证据和设计依据。
- Parts Specialist 负责器件、符号、封装、引脚—焊盘兼容性及可采购性证据。
- Hardware Engineer 通过 Temporal 执行长时间 KiCad、Freerouting 和制造活动。
- Reviewer 独立审查 ERC、DRC、连通性、布局、制造和交付风险。
- AHE 只修复 Harness/流程缺陷；EHE 聚合跨任务的匿名失败签名并生成隔离候选，不直接修改稳定源码。
- 普通设计风险可以交付为 `delivered_with_issues`；工具执行故障才会进入 `execution_blocked`。
- 产物使用内容寻址和 SHA-256 Manifest，支持人工反馈创建新 Revision，不覆盖旧工程。

系统不会把缺失的 KiCad 器件、封装或数据手册编造成“已验证”结果。指定器件不可用时，必须报告真实候选并等待批准替代。

## 总体架构

```mermaid
flowchart LR
    U[浏览器用户] --> P[OAuth2 Proxy\nOIDC 会话]
    P --> F[Next.js 前端\nBFF + fetch/ReadableStream]
    F --> J[Java Spring Control Plane\n认证/租户/任务/产物/SSE]
    J -->|内部 HTTP/SSE 或 gRPC\n短期签名身份| R[Python Agent Runtime]
    R --> L[LangGraph\nSupervisor + 子智能体]
    L --> T[Temporal\nHardware Engineer Workflow]
    T --> K[KiCad CLI / Freerouting\n17 步工程流水线]
    J --> PG[(PostgreSQL\n业务状态/RLS/Outbox)]
    R --> VM[(PostgreSQL + pgvector\n跨会话语义记忆)]
    R --> CP[(PostgreSQL\nLangGraph Checkpoint)]
    R --> RD[(Redis\nLease/Replay/限流/LLM Stream)]
    J --> KF[(Kafka\n审计/用量/生命周期事件)]
    R --> S3[(S3 兼容存储\n工程与制造产物)]
    K --> S3
```

### 服务边界

| 层 | 技术 | 负责内容 |
|---|---|---|
| 浏览器入口 | OAuth2 Proxy + Keycloak | OIDC Authorization Code + PKCE、登录会话、退出登录 |
| Web | Next.js / React / TypeScript | 白色团队工作区、聊天、模型/Profile选择、Markdown渲染、SSE消费、资料页 |
| 控制面 | Java 21 + Spring Boot | OIDC/JWKS验签、租户/RBAC、Project/Run/Revision、配额、Artifact授权、SSE、审计 |
| Agent Runtime | Python + FastAPI | LangGraph运行、内部身份校验、Redis运行协调、事件转换 |
| Agent Kernel | LangGraph | State、节点、handoff、checkpoint、Supervisor和角色协作 |
| 耐久工程执行 | Temporal | Workflow、Activity、重试、超时、Event History、取消和恢复 |
| 持久化 | PostgreSQL | Java业务表、RLS、Outbox、LangGraph checkpoint |
| 长期记忆 | PostgreSQL + pgvector | 用户级事件记忆、显式事实、确定性结果、混合检索和冲突版本 |
| 协调与回放 | Redis | lease、fencing token、幂等、SSE事件回放、LLM输出流 |
| 事件总线 | Kafka | Durable生命周期、审计、用量和EHE observation |
| 文件存储 | S3兼容对象存储 | KiCad、Gerber、BOM、CPL、DSN、SES和审查报告 |

## 登录与请求泳道

```mermaid
sequenceDiagram
    autonumber
    actor User as 用户浏览器
    participant Proxy as OAuth2 Proxy :8088
    participant KC as Keycloak
    participant Web as Next.js :3000
    participant Java as Java Control Plane :8081
    participant Py as Python Runtime :8080/9090

    User->>Proxy: GET /
    Proxy-->>User: 未登录，302 /oauth2/start
    User->>KC: Authorization Code + PKCE S256
    KC-->>Proxy: callback code
    Proxy-->>User: 加密会话 Cookie
    User->>Web: 页面与 BFF 请求
    Web->>Java: /api/v1/session、/info、/runs
    Java->>KC: JWKS 验签 issuer/audience/exp
    Java->>Java: (issuer, subject) + Membership + RLS
    Java->>Py: 内部短期签名任务身份
    Py->>Py: 校验 path/body/run/tenant/project/principal
    Py-->>Java: 事件、里程碑、最终状态
    Java-->>Web: 有序 SSE + Last-Event-ID
    Web-->>User: Markdown、角色进度、产物和风险
```

## AG-UI 前端交互标准

AG-UI（Agent User Interaction）是一个面向 Agent 与前端之间交互事件的通用协议/适配层，通常用于把 Agent 的消息、工具调用、状态更新和流式事件转换为前端可消费的事件流。它适合通用 Copilot 或 Agent UI 集成，但它不是 LangGraph 本身，也不是身份认证协议，更不是 KiCad 执行引擎。

KiCad Design Multi-Agent System 将 AG-UI 作为 Agent 事件的前端交互标准，但不把 AG-UI 当作公网认证入口。正式产品链路固定为：

```text
浏览器 → OAuth2 Proxy → Next.js BFF → Java Control Plane → Python internal_api/gRPC → Agent Kernel（内部标识：ratsnestpro）
```

浏览器仍只连接 Java 的 REST + SSE；Java 把 Python Runtime 的消息、工具证据、状态、产物和人工确认事件规范化为 AG-UI 事件。Next.js 通过 `fetch()` 和 `ReadableStream` 消费有序 `RunEvent`，并用 `Last-Event-ID` 恢复。AG-UI 的 `CUSTOM/ratsnest.human-input-required.v1` 驱动同一 Run 的暂停与恢复；普通 Markdown、reasoning 摘要和工具 JSON 则使用各自的可视组件。这样既获得 AG-UI 的前端生态兼容性，又保持 OIDC、租户、审计和 Run 状态只有一条权威链路。

## 多智能体执行流程

```mermaid
flowchart TD
    A[用户自然语言需求] --> B[Java 创建 Run\n保存 tenant/project/profile 快照]
    B --> C[Intent Router\nbuild/review/feedback/chat]
    C -->|不相关或需澄清| D[返回澄清/普通对话]
    C -->|build| E[Supervisor]
    E --> F[Architect\n检索资料与 KiCad 证据]
    F --> G{符号/封装/引脚焊盘兼容?}
    G -->|缺失| H[请求批准兼容替代\n不编造器件]
    G -->|通过| I[Parts Specialist\n采购和拓扑角色]
    I --> J[Hardware Engineer\nTemporal Workflow]
    J --> K[生成原理图与网络]
    K --> L[生成 PCB 与布局约束]
    L --> M[DSN → Freerouting → SES]
    M --> N[导回 PCB、ERC、DRC、BOM/CPL]
    N --> O[Gerber、钻孔、Manifest]
    O --> P[Reviewer 独立审查]
    P --> Q{是否存在执行级故障?}
    Q -->|是| R[execution_blocked\n保留中间产物]
    Q -->|否| S[delivered_with_issues 或 release_ready]
    S --> T[Java 交付 Manifest、下载授权和审计]
    R --> U[AHE 局部修复\n仅修 Harness 缺陷]
    U --> J
```

## LangGraph 内核

LangGraph 是 Agent 的有向状态图运行时。它将每一步输入、输出、共享 State、消息列表、节点转移和 checkpoint 明确化，适合需要长流程、可恢复、可审计和人工反馈的工程任务。

在 KiCad Design Multi-Agent System 中，Supervisor 是图入口。Architect、Parts Specialist、Hardware Engineer 和 Reviewer 不是互相随意调用的聊天机器人，而是具有边界的阶段节点。确定性工具负责文件、引脚、网络、ERC/DRC 和门禁；LLM 负责资料理解、需求分解、候选选择和解释。

```mermaid
stateDiagram-v2
    [*] --> Intent
    Intent --> Clarify: 非硬件/信息不足
    Intent --> Supervisor: build 或 review
    Supervisor --> Architect
    Architect --> Parts
    Parts --> Hardware
    Hardware --> Reviewer
    Reviewer --> Supervisor: 复审/汇总
    Hardware --> AHE: 可恢复 Harness 故障
    AHE --> Hardware: 预算内继续
    Hardware --> Blocked: 执行级故障
    Reviewer --> DeliveredWithIssues
    Reviewer --> ReleaseReady
    Clarify --> [*]
    Blocked --> [*]
    DeliveredWithIssues --> [*]
    ReleaseReady --> [*]
```

## Hardware Engineer 的 17 步边界

Temporal Workflow 负责耐久编排；具体 KiCad 操作在 Activity 中执行。每个 Activity 有超时、重试和 heartbeat，工作流具有幂等 `run_id + request_id`。

核心阶段包括：

1. 读取 Profile、需求摘要和已验证器件。
2. 生成选型与封装绑定。
3. 验证符号真实存在。
4. 验证封装真实存在。
5. 验证引脚—焊盘兼容。
6. 生成原理图工程。
7. 生成语义网络与连接关系。
8. 生成 PCB 板框、层叠和布局约束。
9. 写入元件并执行确定性布局。
10. 导出 DSN。
11. 真实调用 Freerouting。
12. 生成 SES。
13. 将 SES 导回 PCB。
14. 执行 KiCad ERC。
15. 执行 KiCad DRC 和 unconnected 检查。
16. 导出 BOM、CPL、Gerber 和钻孔文件。
17. Reviewer 独立审查并生成最终 Manifest/报告。

LLM 的布局意图会被编译为带 digest 的 placement constraint sidecar。确定性布局只能在约束允许的区域内工作，不能覆盖 Architect/LLM 已确认的板卡分区。

## AHE 与 EHE

### AHE

AHE（Agentic Harness Engineer）处理框架自身的可恢复缺陷，例如事件格式漂移、状态增量错误、输入映射错误、局部网络生成失败或旧上下文污染。它不把真实器件缺失、硬件规格冲突或 KiCad 工具不可用伪装成成功。

每次任务受总修复次数、同一失败签名次数、墙钟时间和 LLM token 预算限制。预算耗尽后系统保留产物并报告问题，而不是无限循环。

### EHE

EHE（Evolutionary Harness Engineer）把匿名化的跨项目失败签名聚合为受治理候选。每个 Run 固化 Harness 版本；候选只能修改低风险白名单文件，不能读取或修改 sealed holdout/固定 grader，也不能改身份、迁移、部署或发布真值。候选需要经过固定评测、内容摘要绑定和独立人工批准，之后才有资格进入 Kubernetes Canary；系统不会自动合并或自动提升到生产。

### 让 Governed Evolution 真正生效

Evolution 不是“单次任务失败后让 LLM 当场改生产代码”。真实闭环需要同时满足身份、重复性、评测和发布四组条件：

1. 从 clean Git commit 构建不可变 Runtime image，生成 Harness Manifest；manifest 必须绑定 source commit/tree、runtime image、toolchain、contract、policy 和 bundle digest。
2. 平台管理员通过 `/api/v1/platform/harness-versions` 注册版本，审批后配置 canary/rollout；Java 固化到 Run 的 version/channel/manifest 必须与 Python Pod 环境完全一致。`legacy-baseline`、零 digest 和 dirty worktree 都不能做 Trial 基线。
3. 配置三套不同的内容寻址 suite：`optimization.v1.json`、`holdout.v1.json`、`adversarial.v1.json`。`.env.example` 已提供当前三份 suite digest；修改任何 case/manifest 后必须重算并提交新 digest。
4. 启动默认的 durable Runtime→Java ingestion worker，再显式启动 `evolution_worker` 和隔离 evaluator。浏览器是否打开不能影响 observation 入库。
5. 在同一 tenant、同一 Harness version/manifest 下，至少两个独立 Project、两个独立 Run 真实复现同一个 allowlisted Harness failure signature。普通设计错误、器件缺证据、基础设施瞬态故障和同一 Project 的重复 Revision不能凑数。
6. Candidate 达到 `eligible` 后，具备 `ratsnest-platform-admin`/`ratsnest.harness.admin` 的管理员以 `Idempotency-Key + expectedVersion` 调用 `/api/v1/platform/evolution/candidates/{candidateId}:evaluate`，提交经过白名单校验、绑定 base commit 的 PatchPlan/PatchBundle。
7. Temporal 在隔离 worktree/Job 内应用 patch，执行固定 optimization/holdout/adversarial 评测，限制网络、路径、日志、墙钟和资源；签名 callback 只能把 Candidate 推到 `awaiting_approval` 或 `rejected`。
8. 人工按精确 `trialId + reportDigest + expectedVersion` 调用 `:approve`。批准仍不会 merge/push/deploy；发布人员必须 code review、commit、build 新 image/manifest、注册新 Harness、canary 观察后 promote，异常则 rollback。

本地开发启动命令：

```powershell
docker compose --profile evolution up -d --build evolution_worker evolution_evaluator
```

生产环境不得使用本地进程 sandbox；必须采用仓库中的独立低权限、无 Secret、无公网 egress 的 Evolution Job overlay。查看 observation/candidate/trial 使用 `/api/v1/evolution/*`，管理写接口故意没有暴露给普通聊天 UI。

## 状态、并发和恢复

```mermaid
flowchart LR
    R[Java Run 权威状态] --> O[PostgreSQL Outbox]
    O --> K[Kafka Durable Event]
    R --> S[Redis Lease + Fencing]
    S --> X[SSE Replay / Last-Event-ID]
    P[LangGraph State] --> C[PostgreSQL Checkpoint]
    T[Temporal Event History] --> W[Workflow Recovery]
    F[人工反馈] --> V[Revision CAS]
    V --> P
```

- PostgreSQL 保存租户隔离的业务状态和 Run 状态。
- PostgreSQL RLS、`TenantContext` 和 Membership 防止跨租户访问。
- Redis lease/fencing 防止多个 Runtime producer 同时写同一 Run。
- `event_seq`/`state_version` 保证事件单调性；Kafka 使用稳定 event ID 去重。
- SSE 断线使用 `Last-Event-ID` 回放；Redis XREAD 超时被视为空闲 heartbeat，而不是任务失败。
- Java/Python 重启后以 `run_id + request_id` reconciliation，不创建重复 Workflow。
- 人工反馈以 `base_revision` CAS 创建新 Revision，旧产物不可覆盖。

## 记忆系统：短期、长期与防幻觉

KiCad Design Multi-Agent System 把“对话连续性”和“跨会话知识”分开治理，避免把一个无限增长的聊天数组误称为记忆系统。

### 短期记忆

短期记忆属于一个工程会话。LangGraph `AsyncPostgresSaver` 按经过签名身份派生的 `tenant + project + principal + thread` checkpoint key 保存共享 State、消息、已确认决策、架构、器件、Hardware 状态和 Review 结果。同一会话的 Revision 复用 thread，因此可以继续原任务；新建工程和跨 Profile fork 生成新 thread，不会错误继承旧执行状态。

前端历史会话由 Java 的 Project/Run/Revision 记录驱动，不依赖浏览器内存。Redis 只保存活跃 Run 的租约、fencing token 与有界事件回放，不是会话真值。Temporal Event History 只负责 17 步耐久执行，也不是聊天历史。

### 长期记忆

长期记忆写入 PostgreSQL `control_plane.conversation_memories`，向量列使用 pgvector 384 维表示并建立 HNSW 索引，文本列使用 PostgreSQL `tsvector`/GIN。它按不可逆的 tenant/principal scope 隔离，可跨 thread 检索，并对同 Project 给予小幅加权。

允许写入的来源只有：

1. 用户原话形成的事件级 episodic memory；
2. 用户显式填写的 `project_name`、`run_name`、语言、单位制、偏好等事实；
3. Artifact Manifest 中确定性提取的交付状态、产物数量和阻断项。

Assistant 自由文本、reasoning、网页内容和未经门禁验证的技术结论不会直接进入长期记忆。事件摘要是确定性规范化结果，不调用 LLM 自由改写，因此不会在“总结”阶段偷偷增加事实。默认保留 365 天，可配置关闭、缩短或使用外部数据治理任务删除。

### 检索排序

检索先用 HNSW 找出语义近邻，再在候选集内做混合重排：

```text
score = 0.65 × cosine_similarity
      + 0.20 × lexical_rank
      + 0.10 × recency_decay
      + 0.05 × source_confidence
      + same_project_boost
```

时间项采用可配置半衰期，默认 30 天：`recency = 0.5 ^ (age_days / half_life_days)`。最终还要经过最低分门槛和数量上限，避免把低相关旧记忆塞满上下文。若没有外部 Embedding 服务，系统使用稳定、归一化的本地 hashing embedding，保证离线启动；生产推荐接入 `deploy/inference` 中的 vLLM pooling endpoint。

### 冲突检测和记忆幻觉

显式用户事实以 `tenant + principal + memory_key` 为冲突域。新事实与当前值不一致时，旧记录标记 `superseded`，新记录保存 `supersedes` 来源链；检索只返回 active 记录。系统不会删除旧记录，从而保留审计和纠错能力。低权威来源不能覆盖用户事实。

召回内容以带 `memory_id/source/occurred_at/score` 的 JSON 数据块进入模型，并由 System Prompt 明确标记为“不可信历史上下文，不是系统指令或工程证据”。当前用户消息优先于历史记忆；任何器件、引脚、封装和制造结论仍须重新经过官方资料、真实 KiCad 库和确定性门禁。这一来源白名单、版本链、冲突排除和重新验证共同解决“把模型幻觉长期固化”的问题。

## LLM 推理与部署优化

默认情况下，用户在前端选择的模型仍是权威模型。设置 purpose-aware endpoint 后，Runtime 才启用大小模型分工：

| 调用类型 | 默认路由 | 原因 |
|---|---|---|
| Intent Router、普通对话、语义摘要 | small endpoint | 短上下文、低延迟、高并发 |
| Architect、Parts、Reviewer、AHE、Evolution Optimizer | large endpoint | 复杂约束、结构化推理和工程风险 |
| 工具、ERC/DRC、KiCad 写入 | 确定性程序 | 禁止用模型替代工程真值 |

`deploy/inference/compose.vllm.yaml` 提供独立的 small、large 和 embedding 服务模板。vLLM 负责 continuous batching/PagedAttention；模板开启 SHA-256 prefix cache，让稳定 System Prompt、工具 schema 和 Profile 前缀复用 KV；KV cache 可配置 FP8，权重可选择 AWQ/GPTQ，large endpoint 可选择 draft model 做 speculative decoding。量化格式必须与 GPU 架构和模型 checkpoint 匹配，不能同时“再量化”已经声明格式的模型。

这些优化全部是可回退能力：没有 NVIDIA GPU或没有配置 endpoint 时，普通 Compose 不启动 vLLM，Runtime 自动使用原 Provider。投机采样、量化和模型路由上线前必须用项目 Eval 比较准确率、门禁通过率、accepted-token rate、P95 延迟、吞吐、显存与成本，不能仅因为 TPS 提升就发布。

## 扩展专职角色

五个核心角色固定对应真实执行节点。用户最多添加三位专职角色，可以选择电源完整性、信号完整性、EMC/ESD、制造、固件接口，或创建自定义角色。角色配置保存在浏览器团队配置中，并由 BFF 严格校验 `role_id/name/responsibility` 后写入 Run 的不可变 runtime config。

自定义专职角色不是任意获得工具权限的新 Agent。Supervisor 会在 Architect/Parts 与 Hardware/Reviewer 的边界调用有界 specialist consultation；专职角色只获得任务需求、已验证证据和职责说明，输出建议进入共享 State，最终仍由确定性门禁和 Reviewer 决定。编辑团队时，预置角色和自定义角色使用独立计数，避免自定义角色被重复计算后无法继续添加。

如果要通过代码增加一个组织级角色：

1. 在 `frontend/types/team.ts` 增加可选展示元数据；
2. 在 Agent Kernel 定义允许读取的 State 投影和输出 schema；
3. 为它声明工具 allowlist、超时、预算与失败语义；
4. 在 Supervisor 路由和 Reviewer 汇总中接入；
5. 增加至少一个正例和越权负例。

仅在前端增加一张卡片不会自动获得新工具或跳过安全边界。

## Java 控制面架构选择

Java 控制面采用“按业务能力分模块的模块化单体 + Port/Adapter”，而不是全局 `controller/service/impl/mapper` 目录，也不是把每张表拆成微服务。

```text
run/
  api/                 Spring MVC、wire DTO、SSE
  application/         submission/query/interaction/lifecycle use case
  domain/model/        Run、Interaction、DeliveryStatus
  domain/port/         RunStore、RuntimeGateway、OutboxPort
  infrastructure/      JdbcRunStore、HTTP/gRPC Runtime、Kafka adapter
```

这种结构与常规 `service/impl/mapper` 相比，多了显式业务边界和 Port，但不会为了只有一个实现而制造空接口。当前 JDBC SQL 通过 `JdbcClient` adapter 隔离；如果未来复杂动态 SQL、批量映射或团队规范确实需要 MyBatis，只替换 infrastructure persistence adapter，不改变 application/domain/API。HTTP 与 gRPC 也保持同一个 Agent Runtime Port 的两个条件化实现。

现在把 Run、Project、Tenant、Artifact 全拆成微服务并不会更先进：它会把创建 Run 时的一次本地事务拆成分布式事务，同时引入服务发现、重试风暴、链路追踪、消息兼容和补偿逻辑。当前更合理的服务边界已经独立部署：Python Runtime、Temporal Worker、Evolution Evaluator、身份、数据库、消息总线和对象存储。只有具备独立数据所有权、独立扩缩容需求和明确 Saga/Outbox 协议的能力，才允许从 Java 模块中抽取。完整决策见 [ADR-0002](docs/adr/0002-modular-monolith-and-service-boundaries.md)。

## 目录结构

```text
.
├── backend/                         Java Spring Control Plane
│   └── src/main/java/...             身份、租户、Run、Artifact、Outbox、SSE
├── frontend/                        Next.js/React 工作区和 BFF
├── src/agents/ratsnestpro/           LangGraph Supervisor 与子智能体
├── src/service/                     Python internal REST/gRPC、Redis、Kafka、SSE
├── src/core/                        LLM provider 与运行配置
├── src/memory/                       LangGraph checkpoint + 跨会话向量记忆
├── src/ratsnestpro/                  KiCad/EDA/17步确定性流水线
├── contracts/                       REST、SSE、gRPC、Runtime JSON Schema
├── docker/                          Runtime、Frontend、Keycloak、Freerouting 镜像
├── deploy/k8s/                      Cell、HPA、网络策略和可观测性模板
├── docs/                            设计、运行、AHE/EHE 和技术报告
├── compose.yaml                     本地 PostgreSQL/Redis/Kafka/Temporal/MinIO/OIDC
└── .env.example                     脱敏环境变量模板
```

## 本地启动

实际密钥只放在本地 `.env`，不要提交到 Git：

```powershell
Copy-Item .env.example .env
# 编辑 .env，至少配置可用的 LLM 和内部服务密钥

docker compose --profile control-plane --profile identity --profile artifact-store up -d --build
```

Compose 会先幂等预置 pgvector、`ratsnest_app` 与 `ratsnest_migrator`，再用独立的 Flyway 进程执行 V1–V18，成功后才启动 Java 控制面。生产环境仍应由平台预置数据库角色/pgvector extension，并通过独立 Kubernetes Flyway Job 执行迁移。

### 推荐的首次启动检查

```powershell
docker compose --profile control-plane --profile identity --profile artifact-store ps
Invoke-WebRequest http://localhost:8081/actuator/health/readiness
Invoke-WebRequest http://localhost:3000/api/health
Invoke-WebRequest http://localhost:8080/health/ready
```

如果前端显示 `An unexpected server error occurred`，先查询 Java 日志中的 request ID。数据库结构问题必须通过下一条不可变 Flyway migration 修复，禁止手工修改历史 migration。V16 专门修复 V15 event-ingestion trigger 在 `FORCE ROW LEVEL SECURITY` 下以 migrator 身份初始化游标时被拒绝的问题；它没有关闭应用租户 RLS。

### 可选 vLLM

普通开发不需要启动 GPU 服务。需要本地推理优化时，按 [vLLM 部署说明](deploy/inference/README.md) 单独启动 inference overlay，再在 `.env` 配置 small/large/embedding endpoint。continuous batching 与 prefix/KV cache 属于模型服务器能力，不应在每个 Agent 节点重复实现一套缓存。

访问地址：

| 地址 | 用途 |
|---|---|
| `http://localhost:8088` | OAuth2 Proxy 保护后的正式开发入口 |
| `http://localhost:3000` | Next.js 前端直连开发入口 |
| `http://localhost:8081/actuator/health` | Java Control Plane 健康检查 |
| `http://localhost:8180` | Keycloak 开发身份服务 |
| `http://localhost:9001` | MinIO 控制台 |

修改 Python 依赖后需要重新构建 Agent Runtime；修改前端依赖后需要重新构建 Frontend。日常验证优先使用静态检查和轻量 smoke，不要默认启动真实 LLM、KiCad 或 Freerouting。

受治理 Harness Evolution 不随产品默认启动。本地开发 profile 同时启动持密钥但不挂源码的控制器，以及不含业务密钥、负责候选执行的 evaluator：

```powershell
docker compose --profile evolution up -d --build evolution_worker evolution_evaluator
```

本地 evaluator 以只读方式挂载仓库，在独立 volume 建立 detached worktree；这是便于开发的进程隔离，不是恶意代码安全边界。生产 overlay 进一步拆为无 Token 的受信控制器、最小 RBAC 的协调器，以及无 Secret、无网络、无 ServiceAccount Token 的一次性候选 Job。三条路径都不会 merge、push 或 deploy。

## 配置与安全

- `.env`、OIDC client secret、内部签名密钥、AWS/S3 key、Kafka SASL secret 不提交。
- `.env.example` 只包含空值或明确的开发占位值。
- Java 是浏览器唯一业务后端；Python Runtime 不接受浏览器伪造的 `user_id`/`tenant_id`。
- Keycloak 只负责身份认证与用户资料；业务权限由 Java Membership/RBAC/RLS 决定。
- 本地 Compose 是开发环境，不代表已经完成跨区域 HA、真实 Metrics API、TLS 演练或 RPO/RTO 验收。

## 相关文档

- [多智能体内核与硬化](docs/multi-agent-kernel-hardening.md)
- [意图识别、AHE 与 EHE](docs/RATSNESTPRO_INTENT_AHE_EHE_ARCHITECTURE.md)
- [LangGraph + Temporal 架构](docs/RATSNESTPRO_LANGGRAPH_TEMPORAL_ARCHITECTURE.md)
- [生产 Runtime 与恢复](docs/RATSNESTPRO_PRODUCTION_RUNTIME.md)
- [外部 Agentic RAG 接入契约](docs/AGENTIC_RAG_GATEWAY.md)
- [分布式 Runtime](docs/DISTRIBUTED_RUNTIME.md)
- [Harness Canary、Flyway 与回滚手册](docs/HARNESS_CANARY_RUNBOOK.md)
- [完整技术报告](docs/RATSNESTPRO_COMPLETE_PROJECT_TECHNICAL_REPORT_ZH.md)

## 当前已知边界

1. 本地 Compose 已具备组件连通配置，但真实 Kubernetes Metrics API、跨区域 failover/failback、TLS 重启恢复和 RPO/RTO 仍需要在目标集群演练。
2. 交付工程是否满足全部硬件设计规则仍需 Reviewer 和人工硬件工程师确认；系统不会把 `delivered_with_issues` 冒充 `release_ready`。
3. hashing embedding 是离线可用性后备，不等价于高质量语义模型；生产应配置经过中文/硬件语料 Eval 的 384 维 embedding endpoint。
4. Governed Evolution 不会自动 merge、push 或 deploy；候选通过评测后仍需要人工批准、代码审查、构建不可变镜像和 Canary。

## License

项目基于 Joshua Carroll 的 [agent-service-toolkit](https://github.com/JoshuaC215/agent-service-toolkit) 进行扩展，保留原项目 MIT License 与版权声明。改造内容包括 KiCad 硬件设计多智能体内核、耐久执行、证据门禁、Java 控制面及相关工程化能力。详见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。
