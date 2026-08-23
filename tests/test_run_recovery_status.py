from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from schema import RunStatus
from service.grpc_runtime import _run_message
from service.redis_run_registry import RedisRunRegistry, _done_delivery_action
from service.run_registry import RunRegistry
from service.run_ui_snapshot import build_ui_snapshot
from service.service import _effective_run_timeout_seconds


class _RedisStatusFake:
    def __init__(self, *, status: str, lease_until_ms: int) -> None:
        self.status = status
        self.lease_until_ms = lease_until_ms

    async def ping(self) -> bool:
        return True

    async def hgetall(self, _key: str) -> dict[str, str]:
        return self.state()

    def state(self) -> dict[str, str]:
        return {
            "request_id": "request-123",
            "fingerprint": "fingerprint",
            "kind": "stream",
            "agent_id": "ratsnestpro-multi-agent",
            "thread_id": "thread-123",
            "user_id": "owner",
            "timeout_seconds": "60",
            "event_buffer_size": "32",
            "status": self.status,
            "created_at": "2026-08-20T00:00:00+00:00",
            "last_event_id": "7",
            "fencing_token": "3",
            "lease_until_ms": str(self.lease_until_ms),
        }

    async def eval(self, _script: str, _keys: int, *_args: object) -> list[object]:
        state = [value for item in self.state().items() for value in item]
        rows = [["7-0", ["payload", "event"]], ["1-0", ["payload", "event"]]]
        return [state, rows, ["1777000000", "0"], [rows[-1]]]

    async def xrange(self, *_args: object, **_kwargs: object) -> list[tuple[str, dict[str, str]]]:
        return [("1-0", {"payload": "event"})]

    async def xrevrange(
        self, *_args: object, **_kwargs: object
    ) -> list[tuple[str, dict[str, str]]]:
        return [("7-0", {"payload": "event"})]

    async def time(self) -> tuple[int, int]:
        return (1_777_000_000, 0)


def _redis_registry(fake: _RedisStatusFake) -> RedisRunRegistry:
    return RedisRunRegistry(
        max_concurrent=1,
        max_queued=1,
        default_timeout=60,
        heartbeat_seconds=1,
        event_buffer_size=32,
        max_event_bytes=4096,
        retention_seconds=60,
        redis_client=fake,
    )


def test_redis_expired_execution_is_recoverable() -> None:
    checked_ms = 1_777_000_000_000
    record = asyncio.run(
        _redis_registry(
            _RedisStatusFake(status="running", lease_until_ms=checked_ms - 1)
        ).get("request-123", "owner")
    )

    status = RunStatus.model_validate(record.public_dict())
    assert status.newest_event_id == 7
    assert status.execution_lease_active is False
    assert status.recoverable is True
    assert status.lease_expires_at is not None
    assert _run_message(status).recoverable is True


def test_redis_live_execution_is_not_recoverable() -> None:
    checked_ms = 1_777_000_000_000
    record = asyncio.run(
        _redis_registry(
            _RedisStatusFake(status="queued", lease_until_ms=checked_ms + 30_000)
        ).get("request-123", "owner")
    )

    status = RunStatus.model_validate(record.public_dict())
    assert status.execution_lease_active is True
    assert status.recoverable is False


def test_memory_execution_is_live_but_never_durable_recoverable() -> None:
    async def scenario() -> RunStatus:
        registry = RunRegistry(
            max_concurrent=1,
            max_queued=1,
            default_timeout=60,
            heartbeat_seconds=1,
            event_buffer_size=32,
            max_event_bytes=4096,
            retention_seconds=60,
        )
        release = asyncio.Event()

        async def producer(_record: object) -> None:
            await release.wait()

        record, _ = await registry.start(
            request_id="request-123",
            fingerprint="fingerprint",
            kind="stream",
            agent_id="ratsnestpro-multi-agent",
            thread_id="thread-123",
            user_id="owner",
            timeout_seconds=None,
            producer=producer,
        )
        await asyncio.sleep(0)
        status = RunStatus.model_validate(record.public_dict())
        release.set()
        await registry.shutdown()
        return status

    status = asyncio.run(scenario())
    assert status.execution_lease_active is True
    assert status.recoverable is False
    assert status.lease_expires_at is None
    assert status.checked_at <= datetime.now(UTC)


def test_temporal_hardware_run_outlives_the_workflow_deadline(monkeypatch) -> None:
    from core import settings

    monkeypatch.setattr(settings, "RATSNESTPRO_TEMPORAL_ENABLED", True)
    monkeypatch.setattr(settings, "RATSNESTPRO_TEMPORAL_WORKFLOW_TIMEOUT_SECONDS", 36_000)
    monkeypatch.setattr(settings, "RATSNESTPRO_TEMPORAL_GRACEFUL_SHUTDOWN_SECONDS", 30)

    assert _effective_run_timeout_seconds(
        7_200,
        "ratsnestpro-multi-agent",
    ) == 36_330


def test_non_temporal_run_preserves_requested_timeout(monkeypatch) -> None:
    from core import settings

    monkeypatch.setattr(settings, "RATSNESTPRO_TEMPORAL_ENABLED", False)

    assert _effective_run_timeout_seconds(7_200, "ratsnestpro-multi-agent") == 7_200
    assert _effective_run_timeout_seconds(90, "another-agent") == 90


def test_recovered_segment_skips_historical_done_and_delivers_later_events() -> None:
    historical_done = {"payload": "data: [DONE]\n\n", "fencing_token": "1"}
    later_event = {"payload": 'data: {"type":"message"}\n\n', "fencing_token": "2"}
    current_done = {"payload": "data: [DONE]\n\n", "fencing_token": "2"}

    assert (
        _done_delivery_action(
            historical_done,
            status="running",
            current_fence=2,
            newest_event_id=4,
            event_id=2,
        )
        == "skip"
    )
    assert (
        _done_delivery_action(
            later_event,
            status="running",
            current_fence=2,
            newest_event_id=4,
            event_id=3,
        )
        == "deliver"
    )
    assert (
        _done_delivery_action(
            current_done,
            status="running",
            current_fence=2,
            newest_event_id=4,
            event_id=4,
        )
        == "wait"
    )
    assert (
        _done_delivery_action(
            current_done,
            status="completed",
            current_fence=2,
            newest_event_id=4,
            event_id=4,
        )
        == "deliver"
    )


def test_terminal_segment_has_exactly_one_effective_done() -> None:
    candidates = [
        (2, {"payload": "data: [DONE]\n\n", "fencing_token": "1"}),
        (4, {"payload": "data: [DONE]\n\n", "fencing_token": "2"}),
    ]
    actions = [
        _done_delivery_action(
            fields,
            status="completed",
            current_fence=2,
            newest_event_id=4,
            event_id=event_id,
        )
        for event_id, fields in candidates
    ]

    assert actions == ["skip", "deliver"]


def _custom_event(value: dict[str, object]) -> str:
    envelope = {
        "type": "message",
        "content": {"type": "custom", "content": "", "custom_data": value},
    }
    return f"data: {json.dumps(envelope)}\n\n"


def _message_event(value: dict[str, object]) -> str:
    return f'data: {json.dumps({"type": "message", "content": value})}\n\n'


def test_ui_snapshot_projects_roles_pipeline_and_omits_llm_text() -> None:
    events = [
        (1, _custom_event({"kind": "workflow_event", "phase": "architect", "status": "started"})),
        (2, _custom_event({"kind": "workflow_event", "phase": "architect", "status": "completed"})),
        (
            3,
            _custom_event(
                {
                    "kind": "llm_output",
                    "agent": "Hardware Engineer",
                    "phase": "hardware-engineer:schematic_layout",
                    "status": "completed",
                    "content": "must-not-leak",
                }
            ),
        ),
        (
            4,
            _custom_event(
                {
                    "kind": "workflow_event",
                    "phase": "hardware-engineer:temporal",
                    "status": "running",
                    "event_type": "pipeline_step_started",
                    "step_id": "erc",
                    "step_index": 8,
                    "completed_steps": 7,
                    "total_steps": 17,
                }
            ),
        ),
    ]

    snapshot = build_ui_snapshot(events, snapshot_cursor=4, run_status="running")

    assert snapshot["current_role"] == "hardware_engineer"
    assert snapshot["current_phase"] == "erc"
    assert snapshot["pipeline"] == {
        "status": "running",
        "completed_steps": 7,
        "total_steps": 17,
        "current_step": "erc",
        "current_step_index": 8,
    }
    assert snapshot["coverage_complete"] is True
    assert "must-not-leak" not in json.dumps(snapshot)


def test_ui_snapshot_explains_legacy_blocked_pipeline_terminal() -> None:
    events = [
        (
            9,
            _custom_event(
                {
                    "kind": "workflow_event",
                    "phase": "hardware-engineer",
                    "status": "execution_blocked",
                    "detail": "11/17 steps",
                    "error": "bounded wall clock expired",
                    "error_type": "workflow_timeout",
                }
            ),
        )
    ]

    snapshot = build_ui_snapshot(events, snapshot_cursor=9, run_status="failed")

    assert snapshot["current_phase"] == "layout_write"
    assert snapshot["pipeline"]["completed_steps"] == 11
    assert snapshot["pipeline"]["current_step_index"] == 12
    assert snapshot["recent_events"][-1]["detail"] == "bounded wall clock expired"


def test_ui_snapshot_allowlists_linked_temporal_hardware_tool_result() -> None:
    tool_call_id = "call-hardware-1"
    tool_result = {
        "status": "error",
        "outcome": "execution_blocked",
        "completed_steps": 11,
        "total_steps": 17,
        "error": "Temporal Activity failed after bounded retries: Activity task timed out",
        "release_blockers": ["Temporal Activity failed after bounded retries"],
        "temporal": {"last_step": "layout_write", "status": "failed"},
        "untrusted_extra": "must-not-leak",
    }
    events = [
        (
            71,
            _message_event(
                {
                    "type": "ai",
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "ratsnest_temporal_hardware_workflow",
                            "args": {},
                            "id": tool_call_id,
                        }
                    ],
                }
            ),
        ),
        (
            72,
            _message_event(
                {
                    "type": "tool",
                    "content": json.dumps(tool_result),
                    "tool_call_id": tool_call_id,
                }
            ),
        ),
    ]

    snapshot = build_ui_snapshot(
        events,
        snapshot_cursor=72,
        run_status="failed",
        artifact_manifest={
            "manifest_id": "historical-manifest",
            "delivery_status": "release_ready",
            "artifacts": [],
        },
        delivery_status="release_ready",
    )

    assert snapshot["current_phase"] == "layout_write"
    assert snapshot["pipeline"]["completed_steps"] == 11
    assert snapshot["pipeline"]["total_steps"] == 17
    assert snapshot["delivery"]["status"] == "execution_blocked"
    assert snapshot["recent_events"][-1]["detail"].endswith("Activity task timed out")
    assert "must-not-leak" not in json.dumps(snapshot)


def test_legacy_terminal_manifest_completes_supervisor_only_at_closeout() -> None:
    events = [
        (
            1,
            _custom_event(
                {
                    "kind": "workflow_event",
                    "phase": "supervisor",
                    "status": "team_ready",
                }
            ),
        )
    ]
    manifest = {"delivery_status": "execution_blocked", "artifacts": []}

    active = build_ui_snapshot(
        events,
        snapshot_cursor=1,
        run_status="running",
        artifact_manifest=manifest,
    )
    terminal = build_ui_snapshot(
        events,
        snapshot_cursor=1,
        run_status="failed",
        artifact_manifest=manifest,
    )

    assert active["role_statuses"][0]["status"] == "running"
    assert terminal["role_statuses"][0]["status"] == "completed"
