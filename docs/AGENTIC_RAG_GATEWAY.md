# 外部 Agentic RAG 接入契约

RatsNestPro 不把知识库实现复制进 Agent Runtime。现有 Agentic RAG 作为独立、受信的 HTTP 服务接入，Architect、Parts Specialist 与 Reviewer 使用同一个检索契约；未配置或调用失败时，运行时自动退回内置知识与受控 Web 检索。

## 查询契约

Runtime 向 `RATSNEST_KNOWLEDGE_GATEWAY_URL` 发送 `POST application/json`：

```json
{
  "schema_version": "1.0",
  "query": "STM32G070 power decoupling and package evidence",
  "role": "architect",
  "limit": 6,
  "evidence_types": ["datasheet", "application_note", "reference_design"],
  "scope": {
    "principal": "rt1:opaque-principal-scope",
    "tenant": "rt1:opaque-tenant-scope",
    "project": "rt1:opaque-project-scope"
  }
}
```

`scope` 是控制面签名身份派生的不可逆作用域，不是浏览器传入的用户、租户或项目 ID。RAG 服务必须按这三个作用域执行 ACL 过滤。

返回值：

```json
{
  "status": "ok",
  "evidence_sufficient": true,
  "results": [
    {
      "id": "document-chunk-id",
      "title": "STM32G070 datasheet",
      "source": "internal-datasheet-index",
      "source_url": "https://approved.example/doc/123",
      "authority": "official_manufacturer",
      "evidence_type": "datasheet",
      "page": 44,
      "score": 0.92,
      "text": "Grounded excerpt...",
      "content_hash": "sha256-of-source-or-chunk",
      "updated_at": "2026-08-19T00:00:00Z"
    }
  ]
}
```

只有 RAG 明确返回 `evidence_sufficient=true` 且至少有一条有效证据时，Agent 才会跳过该阶段的 Web fallback。`datasheet` 若要成为器件引脚/封装依据，还必须标记 `authority=official_manufacturer`。检索内容始终按不可信输入处理，不能覆盖工具门禁、KiCad 实库检查、ERC/DRC 或 Reviewer 结论。

Parts Specialist 会使用技术文档、历史 BOM、生命周期与已批准替代料证据，但不会把文档检索结果推断成实时库存、价格或交期；实时采购结论仍需要独立目录/供应链接口。

## Docker Compose 挂载

让现有 RAG 容器加入本项目 Compose 网络，并给它固定别名 `agentic-rag`。不需要共享代码目录或数据库卷。

```powershell
docker network connect --alias agentic-rag agent-service-toolkit-main_default <你的RAG容器名>
```

在本项目 `.env` 中设置：

```dotenv
RATSNEST_KNOWLEDGE_GATEWAY_URL=http://agentic-rag:8090/v1/search
RATSNEST_KNOWLEDGE_GATEWAY_TOKEN=<服务间短期或轮换令牌>
RATSNEST_KNOWLEDGE_GATEWAY_TIMEOUT_SECONDS=8
```

如果 RAG 自己也由 Compose 管理，推荐在它的 Compose 文件中声明同一个 external network，并设置 network alias，而不是每次执行 `docker network connect`：

```yaml
services:
  agentic-rag:
    networks:
      ratsnest:
        aliases: [agentic-rag]
networks:
  ratsnest:
    external: true
    name: agent-service-toolkit-main_default
```

重建并替换 Agent Runtime 后生效：

```powershell
docker compose build agent_service
docker compose up -d --no-deps --force-recreate agent_service
```

## Kubernetes 挂载

把 `RATSNEST_KNOWLEDGE_GATEWAY_URL` 设置为 RAG 的 ClusterIP Service DNS，例如 `http://agentic-rag.knowledge.svc.cluster.local:8090/v1/search`；把 `RATSNEST_KNOWLEDGE_GATEWAY_TOKEN` 放入 `ratsnest-runtime-secrets`，不要放入 ConfigMap 或 Git。`primary-region` overlay 已只允许 Runtime 访问带 `ratsnest.io/knowledge-access=true` 标签的 Namespace 的 TCP/8090，因此部署 RAG 前需给它所在的 Namespace 加该标签；不要为此开放全公网出口。

## 运行边界

- URL 是部署配置，不能由用户提示词或模型工具参数覆盖。
- 请求不跟随重定向，响应限制为 1 MB，单条文本限制为 4,000 字符，超时限制为 1–30 秒。
- 网关不可用时 fail-soft：内置知识继续工作，需要外部证据的阶段再受控使用 Web。
- Hardware Engineer 不在 Temporal Activity 中临时检索知识；它只消费冻结后的架构与器件证据，保证 17 步执行可重放。
- 外部知识只能补充证据，不能伪造已安装 KiCad 库条目、产物文件或发布门禁。
