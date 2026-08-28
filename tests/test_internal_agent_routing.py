import asyncio
from types import SimpleNamespace

import pytest
from starlette.routing import Match

from service import internal_api


def test_internal_stream_route_accepts_both_controlled_agent_ids() -> None:
    route = next(route for route in internal_api.router.routes if route.path == "/internal/v1/runs/{agent_id}/stream")

    for agent_id in ("ratsnestpro-multi-agent", "ratsnestpro-single-agent-eval"):
        match, scope = route.matches(
            {"type": "http", "method": "POST", "path": f"/internal/v1/runs/{agent_id}/stream"}
        )
        assert match is Match.FULL
        assert scope["path_params"]["agent_id"] == agent_id

def test_internal_stream_forwards_selected_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def fake_stream(_input, agent_id: str):
        captured["agent_id"] = agent_id
        return "stream-response"

    monkeypatch.setattr("service.service.stream", fake_stream)
    request = internal_api.InternalStreamRequest(
        message="test",
        thread_id="eval-thread",
        request_id="eval-request",
    )
    claims = SimpleNamespace(
        subject="user",
        tenant_id="tenant",
        project_id="project",
        run_id="eval-request",
    )

    response = asyncio.run(
        internal_api.internal_stream(
            "ratsnestpro-single-agent-eval",
            request,
            claims,
        )
    )

    assert response == "stream-response"
    assert captured["agent_id"] == "ratsnestpro-single-agent-eval"
