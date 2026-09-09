"""MCP transport only; evidence decisions remain in knowledge_gateway."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx


async def _search(endpoint, token, payload, timeout, tool_name):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": "Bearer " + token} if token else {}
    async with asyncio.timeout(timeout), httpx.AsyncClient(headers=headers, timeout=timeout, trust_env=False, follow_redirects=False) as client:
        async with streamable_http_client(endpoint, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                available = await session.list_tools()
                if tool_name not in {tool.name for tool in available.tools}:
                    raise ValueError("configured knowledge MCP tool is not advertised")
                result = await session.call_tool(tool_name, payload)
                if result.isError:
                    raise ValueError("knowledge MCP tool returned an error")
                value = getattr(result, "structuredContent", None)
                if value is None:
                    texts = [part.text for part in result.content if part.type == "text"]
                    if len(texts) != 1:
                        raise ValueError("knowledge MCP tool must return one JSON object")
                    value = json.loads(texts[0])
                if not isinstance(value, dict) or len(json.dumps(value).encode()) > 1_000_000:
                    raise ValueError("invalid or oversized knowledge MCP result")
                return value


def search_mcp(endpoint, token, payload, timeout, tool_name):
    # The public gateway is synchronous, including callers inside async graphs.
    # Own the MCP session and event loop in one bounded worker thread.
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_search(endpoint, token, payload, timeout, tool_name))).result()
    except Exception as exc:
        # SDK transport failures may arrive as ExceptionGroup. Do not leak keys,
        # document text or remote error bodies into the user-facing fallback.
        raise ValueError("knowledge MCP request failed: " + type(exc).__name__) from exc
