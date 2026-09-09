"""Optional MCP facade for an existing HTTP Agentic RAG deployment."""
import hmac
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

server = FastMCP("RatsNest knowledge gateway", stateless_http=True, json_response=True,
                 transport_security=TransportSecuritySettings(
                     enable_dns_rebinding_protection=True,
                     allowed_hosts=os.getenv("RATSNEST_MCP_ALLOWED_HOSTS", "knowledge_mcp:*,localhost:*,127.0.0.1:*").split(",")))


@server.tool()
async def search_knowledge(query: str, role: str, limit: int = 5, evidence_types: list[str] | None = None,
                           scope: dict[str, str] | None = None, schema_version: str = "1.0") -> dict:
    """Retrieve source-bound evidence. The upstream service must enforce tenant ACLs."""
    url = os.environ["RATSNEST_RAG_UPSTREAM_URL"]
    token = os.getenv("RATSNEST_RAG_UPSTREAM_TOKEN", "")
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        async with client.stream("POST", url, headers=headers, json={
            "schema_version": schema_version, "query": query[:8000], "role": role[:80],
            "limit": max(1, min(limit, 8)), "evidence_types": (evidence_types or [])[:12], "scope": scope or {},
        }) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 1_000_000:
                    raise ValueError("RAG result too large")
                chunks.append(chunk)
    import json
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise ValueError("RAG must return an evidence object")
    return value


class Authenticate(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        key = os.getenv("RATSNEST_KNOWLEDGE_GATEWAY_TOKEN", "")
        if len(key) < 32 or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + key):
            return JSONResponse({"detail": "authentication required"}, status_code=403)
        return await call_next(request)


app = server.streamable_http_app()
app.add_middleware(Authenticate)
