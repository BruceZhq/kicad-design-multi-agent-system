from __future__ import annotations

import asyncio
import json
from typing import Any

from service import service as runtime_service
from service.ahe_event import ahe_event_record
from service.redis_run_registry import RedisRunRegistry, RunHandle


class _DedupeRedis:
    def __init__(self) -> None:
        self.event_seq = 0
        self.event_keys: list[str] = []
        self._dedupe: dict[str, int] = {}

    async def eval(self, _script: str, _numkeys: int, *args: Any) -> list[int]:
        event_key = str(args[8])
        self.event_keys.append(event_key)
        if event_key and event_key in self._dedupe:
            return [0, self._dedupe[event_key]]
        self.event_seq += 1
        if event_key:
            self._dedupe[event_key] = self.event_seq
        return [1, self.event_seq]


def _runtime_event(*, include_record_id: bool = True) -> tuple[str, dict[str, Any]]:
    record = ahe_event_record(
        {
            "kind": "ahe_event",
            "event": "harness_defect_observed",
            "step": "schematic_connections",
            "revision": 1,
            "failure": {
                "failure_id": "failure-1",
                "signature": "signature-1",
                "origin": "harness",
            },
        },
        workflow_id="workflow-1",
    )
    if not include_record_id:
        record.pop("record_id")
    payload = {
        "type": "message",
        "content": {
            "type": "custom",
            "content": "",
            "custom_data": record,
            "run_id": "graph-run-1",
        },
    }
    return f"data: {json.dumps(payload)}\n\n", record


def _registry(client: _DedupeRedis) -> RedisRunRegistry:
    return RedisRunRegistry(
        max_concurrent=1,
        max_queued=1,
        default_timeout=30,
        heartbeat_seconds=1,
        event_buffer_size=32,
        max_event_bytes=64_000,
        retention_seconds=60,
        redis_client=client,
        instance_id="runtime-1",
    )


def _handle() -> RunHandle:
    return RunHandle(
        request_id="request-1",
        fingerprint="fingerprint-1",
        kind="stream",
        agent_id="ratsnestpro",
        thread_id="thread-1",
        user_id="user-1",
        timeout_seconds=30,
        event_buffer_size=32,
        status="running",
        owner_id="runtime-1",
        fencing_token=1,
    )


def test_runtime_replay_uses_stable_ahe_append_key_without_new_sequence(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        event, record = _runtime_event()
        client = _DedupeRedis()
        registry = _registry(client)
        handle = _handle()

        async def replay(*_args: Any, **_kwargs: Any):
            yield event

        async def set_run_id(current: RunHandle, run_id: str | None) -> None:
            current.run_id = run_id

        monkeypatch.setattr(runtime_service, "message_generator", replay)
        monkeypatch.setattr(runtime_service, "run_registry", registry)
        monkeypatch.setattr(registry, "set_run_id", set_run_id)

        await runtime_service._produce_stream_events(
            handle,
            object(),  # type: ignore[arg-type]
            "ratsnestpro",
        )
        # A recovered producer replays the same durable Temporal record.
        await runtime_service._produce_stream_events(
            handle,
            object(),  # type: ignore[arg-type]
            "ratsnestpro",
        )

        expected_key = f"ahe:{record['record_id']}"
        assert client.event_keys == [expected_key, expected_key]
        assert client.event_seq == 1
        assert handle.next_event_id == 2
        assert handle.newest_event_id == 1

    asyncio.run(exercise())


def test_runtime_drops_ahe_event_without_record_id_instead_of_allocating_sequence(
    monkeypatch,
) -> None:
    async def exercise() -> None:
        event, _ = _runtime_event(include_record_id=False)
        client = _DedupeRedis()
        registry = _registry(client)
        handle = _handle()

        async def replay(*_args: Any, **_kwargs: Any):
            yield event

        async def set_run_id(current: RunHandle, run_id: str | None) -> None:
            current.run_id = run_id

        monkeypatch.setattr(runtime_service, "message_generator", replay)
        monkeypatch.setattr(runtime_service, "run_registry", registry)
        monkeypatch.setattr(registry, "set_run_id", set_run_id)

        await runtime_service._produce_stream_events(
            handle,
            object(),  # type: ignore[arg-type]
            "ratsnestpro",
        )

        assert client.event_keys == []
        assert client.event_seq == 0
        assert handle.next_event_id == 1

    asyncio.run(exercise())
