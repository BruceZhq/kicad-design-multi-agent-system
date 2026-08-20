"""Bounded, idempotent lifecycle management for agent runs."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException

from service.run_ui_snapshot import build_ui_snapshot
from service.sse import format_buffered_sse

RunKind = Literal["invoke", "stream"]
RunState = Literal[
    "queued",
    "running",
    "waiting_for_input",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]
Producer = Callable[["RunRecord"], Awaitable[Any]]

logger = logging.getLogger(__name__)
_TERMINAL_STATES = {"completed", "failed", "cancelled", "timed_out"}


class RunConflictError(Exception):
    pass


class RunOverloadedError(Exception):
    pass


class RunNotFoundError(Exception):
    pass


class RunAccessError(Exception):
    pass


class InteractionConflictError(Exception):
    pass


class InvalidRunTransitionError(Exception):
    pass


@dataclass
class RunRecord:
    request_id: str
    fingerprint: str
    kind: RunKind
    agent_id: str
    thread_id: str
    user_id: str | None
    timeout_seconds: float
    event_buffer_size: int
    status: RunState = "queued"
    run_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error_code: str | None = None
    error: str | None = None
    http_status: int = 500
    stream_failed: bool = False
    terminal_event_emitted: bool = False
    interaction_id: str | None = None
    interaction_state_version: int | None = None
    accepted_responses: dict[str, tuple[str, int]] = field(default_factory=dict)
    event_keys: dict[str, int] = field(default_factory=dict)
    execution_generation: int = 1
    events: deque[tuple[int, str]] = field(init=False)
    next_event_id: int = 1
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.events = deque(maxlen=self.event_buffer_size)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATES

    def public_dict(self) -> dict[str, Any]:
        checked_at = datetime.now(UTC)
        oldest = self.events[0][0] if self.events else None
        newest = self.events[-1][0] if self.events else None
        result = self.result if isinstance(self.result, dict) else {}
        ui_snapshot = build_ui_snapshot(
            list(self.events),
            snapshot_cursor=self.next_event_id - 1,
            run_status=self.status,
            artifact_manifest=result.get("artifact_manifest"),
            delivery_status=result.get("delivery_status"),
        )
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "agent_id": self.agent_id,
            "thread_id": self.thread_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "event_count": self.next_event_id - 1,
            "oldest_event_id": oldest,
            "newest_event_id": newest,
            "execution_lease_active": (
                self.status in {"queued", "running"}
                and self.task is not None
                and not self.task.done()
            ),
            # The in-memory backend cannot survive a process restart. Only the
            # Redis backend can advertise a durable run as recoverable.
            "recoverable": False,
            "lease_expires_at": None,
            "checked_at": checked_at,
            "error_code": self.error_code,
            "error": self.error,
            "interaction_id": self.interaction_id,
            "interaction_state_version": self.interaction_state_version,
            "artifact_manifest": result.get("artifact_manifest"),
            "ui_snapshot": ui_snapshot,
            "delivery_status": result.get("delivery_status"),
        }


class RunRegistry:
    """Own background execution independently from HTTP client connections."""

    def __init__(
        self,
        *,
        max_concurrent: int,
        max_queued: int,
        default_timeout: float,
        heartbeat_seconds: float,
        event_buffer_size: int,
        max_event_bytes: int,
        retention_seconds: float,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_queued = max_queued
        self.default_timeout = default_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self.event_buffer_size = event_buffer_size
        self.max_event_bytes = max_event_bytes
        self.retention_seconds = retention_seconds
        self._records: dict[str, RunRecord] = {}
        self._guard = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def startup(self) -> None:
        """Initialize external resources; the in-memory backend has none."""

    async def healthcheck(self) -> dict[str, str]:
        return {"backend": "memory", "status": "ready"}

    async def wait_terminal(self, record: RunRecord) -> RunRecord:
        """Wait for terminal state through the backend's coordination primitive."""

        await asyncio.shield(record.done.wait())
        return record

    async def set_run_id(self, record: RunRecord, run_id: str | None) -> None:
        if run_id and not record.run_id:
            record.run_id = run_id

    async def mark_stream_failed(
        self,
        record: RunRecord,
        *,
        code: str,
        message: str,
    ) -> None:
        record.stream_failed = True
        record.error_code = code
        record.error = message

    async def start(
        self,
        *,
        request_id: str,
        fingerprint: str,
        kind: RunKind,
        agent_id: str,
        thread_id: str,
        user_id: str | None,
        timeout_seconds: float | None,
        producer: Producer,
    ) -> tuple[RunRecord, bool]:
        async with self._guard:
            self._cleanup_locked()
            existing = self._records.get(request_id)
            if existing is not None:
                self._validate_existing(existing, fingerprint, kind, user_id)
                return existing, False

            pending = sum(
                not record.is_terminal and record.status != "waiting_for_input"
                for record in self._records.values()
            )
            if pending >= self.max_concurrent + self.max_queued:
                raise RunOverloadedError(
                    "The agent run queue is full; retry after an active run completes."
                )

            effective_timeout = min(
                timeout_seconds or self.default_timeout,
                self.default_timeout,
            )
            record = RunRecord(
                request_id=request_id,
                fingerprint=fingerprint,
                kind=kind,
                agent_id=agent_id,
                thread_id=thread_id,
                user_id=user_id,
                timeout_seconds=effective_timeout,
                event_buffer_size=self.event_buffer_size,
            )
            self._records[request_id] = record
            record.task = asyncio.create_task(
                self._execute(record, producer, record.execution_generation),
                name=f"agent-run:{request_id}",
            )
            return record, True

    async def pause_for_input(
        self,
        record: RunRecord,
        *,
        interaction_id: str,
        state_version: int,
        payload: str,
    ) -> RunRecord:
        """Atomically publish a question and release execution for its answer."""

        if len(payload.encode("utf-8")) > self.max_event_bytes:
            raise InvalidRunTransitionError(
                f"Human-input event exceeded the {self.max_event_bytes}-byte service limit."
            )

        async with self._guard:
            current = self._records.get(record.request_id)
            if current is None:
                raise RunNotFoundError(record.request_id)
            if current.status == "waiting_for_input":
                if (
                    current.interaction_id == interaction_id
                    and current.interaction_state_version == state_version
                ):
                    return current
                raise InteractionConflictError("Run is waiting for a different interaction.")
            if current.status != "running":
                raise InvalidRunTransitionError(
                    f"Cannot pause a run in state {current.status!r}."
                )
            current.status = "waiting_for_input"
            current.interaction_id = interaction_id
            current.interaction_state_version = state_version
            current.finished_at = None
            current.done.clear()
            event_id = current.next_event_id
            current.next_event_id += 1
            current.events.append((event_id, payload))
        await self._notify(record)
        return record

    async def resume(
        self,
        *,
        request_id: str,
        user_id: str | None,
        interaction_id: str,
        response_request_id: str,
        state_version: int,
        producer: Producer,
    ) -> tuple[RunRecord, bool]:
        """CAS a waiting run to queued and schedule its checkpoint continuation."""

        async with self._guard:
            self._cleanup_locked()
            record = self._records.get(request_id)
            if record is None:
                raise RunNotFoundError(request_id)
            self._validate_owner(record, user_id)
            response_identity = (interaction_id, state_version)
            prior = record.accepted_responses.get(response_request_id)
            if prior is not None:
                if prior != response_identity:
                    raise InteractionConflictError(
                        "response_request_id was already used for another interaction."
                    )
                return record, False
            if record.status != "waiting_for_input":
                raise InvalidRunTransitionError(
                    f"Cannot resume a run in state {record.status!r}."
                )
            if record.interaction_id != interaction_id:
                raise InteractionConflictError("interaction_id does not match the pending request.")
            if record.interaction_state_version != state_version:
                raise InteractionConflictError("Interaction state_version is stale.")

            record.accepted_responses[response_request_id] = response_identity
            record.status = "queued"
            record.interaction_id = None
            record.interaction_state_version = None
            record.finished_at = None
            record.error_code = None
            record.error = None
            record.done.clear()
            record.execution_generation += 1
            generation = record.execution_generation
            task = asyncio.create_task(
                self._execute(record, producer, generation),
                name=f"agent-run-resume:{request_id}",
            )
            record.task = task
        await self._notify(record)
        return record, True

    async def _execute(
        self,
        record: RunRecord,
        producer: Producer,
        generation: int,
    ) -> None:
        try:
            async with self._semaphore:
                async with self._guard:
                    if record.execution_generation != generation or record.status != "queued":
                        return
                    record.status = "running"
                    record.started_at = record.started_at or datetime.now(UTC)
                await self._notify(record)
                async with asyncio.timeout(record.timeout_seconds):
                    result = await producer(record)
                if (
                    record.execution_generation != generation
                    or record.status == "waiting_for_input"
                ):
                    return
                record.result = result
                if record.kind == "stream" and not record.terminal_event_emitted:
                    await self.append_event(record, "data: [DONE]\n\n")
                record.status = "failed" if record.stream_failed else "completed"
                if record.stream_failed:
                    record.error_code = record.error_code or "stream_error"
                    record.error = record.error or "The agent stream reported an error."
        except TimeoutError:
            if record.status == "waiting_for_input":
                return
            record.status = "timed_out"
            record.error_code = "run_timeout"
            record.error = f"Run exceeded {record.timeout_seconds:g} seconds."
            record.http_status = 504
            await self._emit_terminal_stream_error(record)
        except asyncio.CancelledError:
            record.status = "cancelled"
            record.error_code = "run_cancelled"
            record.error = "Run was cancelled."
            record.http_status = 409
            await self._emit_terminal_stream_error(record)
        except HTTPException as exc:
            if record.status == "waiting_for_input":
                return
            record.status = "failed"
            record.error_code = "request_rejected"
            record.error = str(exc.detail)
            record.http_status = exc.status_code
            await self._emit_terminal_stream_error(record)
        except Exception:
            if record.status == "waiting_for_input":
                return
            logger.exception("Unhandled agent run failure request_id=%s", record.request_id)
            record.status = "failed"
            record.error_code = "internal_error"
            record.error = "Internal server error"
            record.http_status = 500
            await self._emit_terminal_stream_error(record)
        finally:
            if record.is_terminal:
                record.finished_at = datetime.now(UTC)
                record.done.set()
            await self._notify(record)

    async def append_event(
        self,
        record: RunRecord,
        payload: str,
        *,
        event_key: str | None = None,
    ) -> int:
        if record.status == "waiting_for_input" and "data: [DONE]" in payload:
            raise InvalidRunTransitionError(
                "A run waiting for input cannot emit a terminal stream marker."
            )
        if event_key and event_key in record.event_keys:
            return record.event_keys[event_key]
        encoded_size = len(payload.encode("utf-8"))
        if encoded_size > self.max_event_bytes:
            record.stream_failed = True
            record.error_code = "event_too_large"
            record.error = f"Stream event exceeded the {self.max_event_bytes}-byte service limit."
            payload = self._error_payload(record.error, record.error_code, retryable=False)
        event_id = record.next_event_id
        record.next_event_id += 1
        record.events.append((event_id, payload))
        if event_key:
            record.event_keys[event_key] = event_id
        if "data: [DONE]" in payload:
            record.terminal_event_emitted = True
        await self._notify(record)
        return event_id

    async def subscribe(
        self,
        record: RunRecord,
        *,
        last_event_id: int,
    ) -> AsyncGenerator[str, None]:
        newest_event_id = record.next_event_id - 1
        if last_event_id > newest_event_id:
            yield self._format_event(
                last_event_id + 1,
                self._error_payload(
                    "The requested replay position is ahead of the current run; reload thread history.",
                    "replay_gap",
                    retryable=False,
                ),
            )
            return
        if record.events and last_event_id < record.events[0][0] - 1:
            yield self._format_event(
                record.events[0][0] - 1,
                self._error_payload(
                    "The requested replay position is no longer buffered; reload thread history.",
                    "replay_gap",
                    retryable=False,
                ),
            )
            return

        cursor = last_event_id
        while True:
            available: list[tuple[int, str]]
            terminal: bool
            heartbeat = False
            async with record.condition:
                available = [event for event in record.events if event[0] > cursor]
                terminal = record.is_terminal
                waiting = record.status == "waiting_for_input"
                if not available and not terminal and not waiting:
                    try:
                        await asyncio.wait_for(
                            record.condition.wait(),
                            timeout=self.heartbeat_seconds,
                        )
                    except TimeoutError:
                        heartbeat = True
            if heartbeat:
                yield ": heartbeat\n\n"
                continue
            if not available and not terminal and not waiting:
                continue

            for event_id, payload in available:
                cursor = event_id
                yield self._format_event(event_id, payload)
            if terminal and cursor >= record.next_event_id - 1:
                return
            if waiting and cursor >= record.next_event_id - 1:
                return

    async def get(self, request_id: str, user_id: str | None) -> RunRecord:
        async with self._guard:
            self._cleanup_locked()
            record = self._records.get(request_id)
            if record is None:
                raise RunNotFoundError(request_id)
            self._validate_owner(record, user_id)
            return record

    async def cancel(self, request_id: str, user_id: str | None) -> RunRecord:
        record = await self.get(request_id, user_id)
        if record.is_terminal:
            return record
        if record.task is not None and not record.task.done():
            record.error_code = "cancel_requested"
            record.error = "Cancellation requested."
            record.task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(record.done.wait()),
                    timeout=2,
                )
            except TimeoutError:
                pass
        elif record.status == "waiting_for_input":
            record.status = "cancelled"
            record.error_code = "run_cancelled"
            record.error = "Run was cancelled."
            record.http_status = 409
            record.finished_at = datetime.now(UTC)
            await self._emit_terminal_stream_error(record)
            record.done.set()
            await self._notify(record)
        return record

    async def shutdown(self) -> None:
        async with self._guard:
            tasks = [
                record.task
                for record in self._records.values()
                if record.task is not None and not record.task.done()
            ]
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def metrics(self) -> dict[str, int]:
        async with self._guard:
            self._cleanup_locked()
            counts = {
                state: 0
                for state in (
                    "queued",
                    "running",
                    "waiting_for_input",
                    "completed",
                    "failed",
                    "cancelled",
                    "timed_out",
                )
            }
            for record in self._records.values():
                counts[record.status] += 1
            return {
                **counts,
                "retained": len(self._records),
                "capacity": self.max_concurrent,
                "queue_capacity": self.max_queued,
            }

    async def _emit_terminal_stream_error(self, record: RunRecord) -> None:
        if record.kind != "stream":
            return
        await self.append_event(
            record,
            self._error_payload(
                record.error or "Run failed.",
                record.error_code or "run_failed",
                retryable=record.status in {"timed_out"},
            ),
        )
        if not record.terminal_event_emitted:
            await self.append_event(record, "data: [DONE]\n\n")

    async def _notify(self, record: RunRecord) -> None:
        async with record.condition:
            record.condition.notify_all()

    def _cleanup_locked(self) -> None:
        now = datetime.now(UTC)
        expired = [
            request_id
            for request_id, record in self._records.items()
            if record.finished_at is not None
            and (now - record.finished_at).total_seconds() > self.retention_seconds
        ]
        for request_id in expired:
            del self._records[request_id]

    @staticmethod
    def _validate_existing(
        record: RunRecord,
        fingerprint: str,
        kind: RunKind,
        user_id: str | None,
    ) -> None:
        RunRegistry._validate_owner(record, user_id)
        if record.fingerprint != fingerprint or record.kind != kind:
            raise RunConflictError("request_id was already used for a different request.")

    @staticmethod
    def _validate_owner(record: RunRecord, user_id: str | None) -> None:
        if record.user_id and record.user_id != user_id:
            raise RunAccessError(record.request_id)

    @staticmethod
    def _format_event(event_id: int, payload: str) -> str:
        return format_buffered_sse(event_id, payload)

    @staticmethod
    def _error_payload(
        message: str,
        code: str,
        *,
        retryable: bool,
    ) -> str:
        import json

        return (
            "data: "
            + json.dumps(
                {
                    "type": "error",
                    "content": message,
                    "code": code,
                    "retryable": retryable,
                }
            )
            + "\n\n"
        )
