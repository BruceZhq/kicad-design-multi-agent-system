"""Redis-backed, cross-instance lifecycle management for agent runs.

The registry deliberately keeps execution local: Python ``producer`` callables are
not serializable and are never sent through Redis or Kafka. Redis is the durable
coordination and SSE replay plane. Hardware work remains the responsibility of
Temporal.

Importing this module does not import the Redis runtime, create a connection, or
start background work. ``redis.asyncio`` is loaded lazily by :meth:`startup`, and
a Redis-compatible client can be injected for focused unit tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from service.kafka_audit import KafkaAuditEvent
from service.run_registry import (
    InteractionConflictError,
    InvalidRunTransitionError,
    RunAccessError,
    RunConflictError,
    RunKind,
    RunNotFoundError,
    RunOverloadedError,
    RunState,
)
from service.run_ui_snapshot import build_ui_snapshot
from service.runtime_identity import audit_scopes
from service.sse import format_buffered_sse

Producer = Callable[["RunHandle"], Awaitable[Any]]

_TERMINAL_STATES = {"completed", "failed", "cancelled", "timed_out"}
_REGISTRY_SLOT = "{registry}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _decoded_hash(values: Mapping[Any, Any]) -> dict[str, str]:
    return {_text(key): _text(value) for key, value in values.items()}


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_done_payload(payload: str) -> bool:
    return payload.strip() == "data: [DONE]"


def _done_delivery_action(
    fields: Mapping[str, str],
    *,
    status: str,
    current_fence: int,
    newest_event_id: int,
    event_id: int,
) -> str:
    """Classify a DONE marker without deleting its durable audit history."""

    payload = fields.get("payload", "")
    if not _is_done_payload(payload):
        return "deliver"
    terminal = status in _TERMINAL_STATES
    event_fence = fields.get("fencing_token")
    if event_fence is None:
        # Backward compatibility for terminal runs written before events were
        # fenced. A legacy DONE is valid only when it is the final event.
        return "deliver" if terminal and event_id == newest_event_id else "skip"
    try:
        fence = int(event_fence)
    except ValueError:
        return "skip"
    if fence != current_fence:
        return "skip"
    if not terminal:
        # The writer appends DONE immediately before committing terminal state.
        # Hold the cursor so this marker can be re-read after that commit, or
        # invalidated by a fenced takeover if the writer dies in between.
        return "wait"
    return "deliver" if event_id == newest_event_id else "skip"


# HGETALL, XRANGE and TIME execute in one Redis script, so the event projection
# is pinned to the same high-water mark as the lifecycle state.
_LOAD_STATUS_SNAPSHOT_LUA = r"""
local state = redis.call('HGETALL', KEYS[1])
if #state == 0 then return {state, {}, redis.call('TIME')} end
local cursor = tonumber(redis.call('HGET', KEYS[1], 'last_event_id') or '0')
local max_id = cursor .. '-0'
local events = redis.call('XREVRANGE', KEYS[2], max_id, '-', 'COUNT', 256)
local oldest = redis.call('XRANGE', KEYS[2], '-', max_id, 'COUNT', 1)
return {state, events, redis.call('TIME'), oldest}
"""


def _decode_flat_pairs(values: list[Any]) -> dict[str, str]:
    return {
        _text(values[index]): _text(values[index + 1])
        for index in range(0, len(values), 2)
    }


def _decode_snapshot_rows(values: list[Any]) -> list[tuple[str, dict[str, str]]]:
    return [
        (_text(row[0]), _decode_flat_pairs(list(row[1])))
        for row in values
        if isinstance(row, (list, tuple)) and len(row) == 2
    ]


@dataclass
class RunHandle:
    """Portable run snapshot plus local-only compatibility handles."""

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
    created_at: datetime = field(default_factory=_utcnow)
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
    next_event_id: int = 1
    oldest_event_id: int | None = None
    newest_event_id: int | None = None
    owner_id: str = ""
    fencing_token: int = 0
    lease_until_ms: int = 0
    execution_lease_active: bool = False
    recoverable: bool = False
    lease_expires_at: datetime | None = None
    checked_at: datetime = field(default_factory=_utcnow)
    ui_snapshot: dict[str, Any] = field(default_factory=dict)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATES

    def public_dict(self) -> dict[str, Any]:
        result = self.result if isinstance(self.result, dict) else {}
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
            "oldest_event_id": self.oldest_event_id,
            "newest_event_id": self.newest_event_id,
            "execution_lease_active": self.execution_lease_active,
            "recoverable": self.recoverable,
            "lease_expires_at": self.lease_expires_at,
            "checked_at": self.checked_at,
            "error_code": self.error_code,
            "error": self.error,
            "interaction_id": self.interaction_id,
            "interaction_state_version": self.interaction_state_version,
            "artifact_manifest": result.get("artifact_manifest"),
            "ui_snapshot": self.ui_snapshot,
            "delivery_status": result.get("delivery_status"),
        }


# KEYS: state, lifecycle, audit-outbox, active, running, metrics
# ARGV: request fields, limits, owner, lease, timestamps and audit id.
_CREATE_OR_GET_LUA = r"""
local state = KEYS[1]
local lifecycle = KEYS[2]
local audit = KEYS[3]
local active = KEYS[4]
local running = KEYS[5]
local metrics = KEYS[6]
local clock = redis.call('TIME')
local now_ms = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
local identity_scope = ''
if string.sub(ARGV[6], 1, 4) == 'rt1:' then identity_scope = ARGV[6] end

if redis.call('EXISTS', state) == 1 then
  local stored_user = redis.call('HGET', state, 'user_id') or ''
  if stored_user ~= '' and stored_user ~= ARGV[6] then return {-2, 0} end
  if redis.call('HGET', state, 'fingerprint') ~= ARGV[2]
     or redis.call('HGET', state, 'kind') ~= ARGV[3] then
    return {-1, 0}
  end

  local status = redis.call('HGET', state, 'status') or ''
  if status == 'waiting_for_input' then
    return {0, tonumber(redis.call('HGET', state, 'fencing_token') or '0')}
  end
  if status == 'completed' or status == 'failed'
     or status == 'cancelled' or status == 'timed_out' then
    return {0, tonumber(redis.call('HGET', state, 'fencing_token') or '0')}
  end

  local lease_until = tonumber(redis.call('HGET', state, 'lease_until_ms') or '0')
  if lease_until <= now_ms then
    local old_status = status
    local fence = redis.call('HINCRBY', state, 'fencing_token', 1)
    local new_lease = now_ms + tonumber(ARGV[11])
    redis.call('HSET', state,
      'owner_id', ARGV[10],
      'lease_until_ms', new_lease,
      'status', 'queued',
      'started_at', '',
      'finished_at', '',
      'error_code', '',
      'error', '',
      'http_status', '500',
      'terminal_event_emitted', '0',
      'state_version', tonumber(redis.call('HGET', state, 'state_version') or '0') + 1)
    redis.call('ZADD', active, new_lease, state)
    redis.call('ZREM', running, state)
    if old_status ~= 'queued' then
      redis.call('HINCRBY', metrics, old_status, -1)
      redis.call('HINCRBY', metrics, 'queued', 1)
    end
    redis.call('XADD', lifecycle, '*',
      'type', 'lease_taken_over', 'status', 'queued', 'timestamp', ARGV[12])
    redis.call('XADD', audit, '*',
      'event_id', ARGV[14], 'schema_version', '1',
      'event_type', 'RunLeaseTakenOver', 'request_id', ARGV[1],
      'agent_id', ARGV[4], 'thread_id', ARGV[5], 'status', 'queued',
      'identity_scope', identity_scope,
      'timestamp', ARGV[12], 'owner_id', ARGV[10], 'fencing_token', fence)
    return {2, fence}
  end
  return {0, tonumber(redis.call('HGET', state, 'fencing_token') or '0')}
end

local live = redis.call('ZCOUNT', active, '(' .. now_ms, '+inf')
if live >= tonumber(ARGV[13]) then return {-3, 0} end
local lease_until = now_ms + tonumber(ARGV[11])
redis.call('HSET', state,
  'request_id', ARGV[1], 'fingerprint', ARGV[2], 'kind', ARGV[3],
  'agent_id', ARGV[4], 'thread_id', ARGV[5], 'user_id', ARGV[6],
  'timeout_seconds', ARGV[7], 'event_buffer_size', ARGV[8],
  'status', 'queued', 'run_id', '', 'created_at', ARGV[12],
  'started_at', '', 'finished_at', '', 'result_json', '',
  'error_code', '', 'error', '', 'http_status', '500',
  'stream_failed', '0', 'terminal_event_emitted', '0',
  'interaction_id', '', 'interaction_state_version', '',
  'last_event_id', '0', 'owner_id', ARGV[10], 'fencing_token', '1',
  'lease_until_ms', lease_until, 'state_version', '1')
redis.call('ZADD', active, lease_until, state)
redis.call('HINCRBY', metrics, 'queued', 1)
redis.call('HINCRBY', metrics, 'retained', 1)
redis.call('XADD', lifecycle, '*',
  'type', 'status', 'status', 'queued', 'timestamp', ARGV[12])
redis.call('XADD', audit, '*',
  'event_id', ARGV[14], 'schema_version', '1',
  'event_type', 'RunAccepted', 'request_id', ARGV[1],
  'agent_id', ARGV[4], 'thread_id', ARGV[5], 'status', 'queued',
  'identity_scope', identity_scope,
  'timestamp', ARGV[12], 'owner_id', ARGV[10], 'fencing_token', '1')
return {1, 1}
"""


# KEYS: state, events, dedupe, audit-outbox
# ARGV: owner, fence, payload, maxlen, event-key, terminal flag, audit payload
_APPEND_EVENT_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, 0} end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return {-2, 0}
end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status == 'completed' or status == 'failed'
   or status == 'cancelled' or status == 'timed_out' then return {-3, 0} end
if status == 'waiting_for_input' and ARGV[6] == '1' then return {-4, 0} end
if ARGV[5] ~= '' then
  local prior = redis.call('HGET', KEYS[3], ARGV[5])
  if prior then return {0, tonumber(prior)} end
end
local event_id = redis.call('HINCRBY', KEYS[1], 'last_event_id', 1)
redis.call('XADD', KEYS[2], event_id .. '-0',
  'payload', ARGV[3], 'fencing_token', ARGV[2], 'terminal', ARGV[6])
redis.call('XTRIM', KEYS[2], 'MAXLEN', tonumber(ARGV[4]))
if ARGV[5] ~= '' then redis.call('HSET', KEYS[3], ARGV[5], event_id) end
if ARGV[6] == '1' then redis.call('HSET', KEYS[1], 'terminal_event_emitted', '1') end
if ARGV[7] ~= '' then redis.call('XADD', KEYS[4], '*', 'payload', ARGV[7]) end
return {1, event_id}
"""


# KEYS: state, lifecycle, audit, active, running, metrics
# ARGV: owner, fence, max-running, lease-ms, timestamp, audit-id
_ACQUIRE_RUNNING_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local identity_scope = redis.call('HGET', KEYS[1], 'user_id') or ''
if string.sub(identity_scope, 1, 4) ~= 'rt1:' then identity_scope = '' end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return -2
end
if redis.call('HGET', KEYS[1], 'status') ~= 'queued' then return -3 end
local clock = redis.call('TIME')
local now_ms = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
if redis.call('ZCOUNT', KEYS[5], '(' .. now_ms, '+inf') >= tonumber(ARGV[3]) then
  return 0
end
local lease_until = now_ms + tonumber(ARGV[4])
redis.call('HSET', KEYS[1],
  'status', 'running', 'started_at', ARGV[5], 'lease_until_ms', lease_until,
  'state_version', tonumber(redis.call('HGET', KEYS[1], 'state_version') or '0') + 1)
redis.call('ZADD', KEYS[4], lease_until, KEYS[1])
redis.call('ZADD', KEYS[5], lease_until, KEYS[1])
redis.call('HINCRBY', KEYS[6], 'queued', -1)
redis.call('HINCRBY', KEYS[6], 'running', 1)
redis.call('XADD', KEYS[2], '*',
  'type', 'status', 'status', 'running', 'timestamp', ARGV[5])
redis.call('XADD', KEYS[3], '*',
  'event_id', ARGV[6], 'schema_version', '1',
  'event_type', 'RunStarted',
  'request_id', redis.call('HGET', KEYS[1], 'request_id'),
  'agent_id', redis.call('HGET', KEYS[1], 'agent_id'),
  'thread_id', redis.call('HGET', KEYS[1], 'thread_id'),
  'identity_scope', identity_scope,
  'status', 'running', 'timestamp', ARGV[5],
  'owner_id', ARGV[1], 'fencing_token', ARGV[2])
return 1
"""


# KEYS: state, active, running
# ARGV: owner, fence, lease-ms
_HEARTBEAT_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return 0
end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status == 'waiting_for_input' then return 0 end
if status == 'completed' or status == 'failed'
   or status == 'cancelled' or status == 'timed_out' then return 0 end
local clock = redis.call('TIME')
local now_ms = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
local lease_until = now_ms + tonumber(ARGV[3])
redis.call('HSET', KEYS[1], 'lease_until_ms', lease_until)
redis.call('ZADD', KEYS[2], lease_until, KEYS[1])
if status == 'running' then redis.call('ZADD', KEYS[3], lease_until, KEYS[1]) end
return 1
"""


# KEYS: state, active, running
# ARGV: owner, fence
#
# A process shutdown is not a user cancellation. Expire only this owner's
# execution lease so another Runtime instance can immediately attach/take over
# the durable run. Fencing prevents a stale process from releasing a newer
# owner's lease.
_RELEASE_LEASE_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return 0
end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status == 'waiting_for_input' then return 1 end
if status == 'completed' or status == 'failed'
   or status == 'cancelled' or status == 'timed_out' then return 0 end
redis.call('HSET', KEYS[1], 'lease_until_ms', '0')
redis.call('ZADD', KEYS[2], 0, KEYS[1])
redis.call('ZADD', KEYS[3], 0, KEYS[1])
return 1
"""


# KEYS: state, lifecycle, audit, active, running, metrics, events, dedupe
# ARGV: owner, fence, target status, timestamp, result-json, error-code,
#       error, http-status, retention-ms, audit-id, detail-digest
_TRANSITION_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local identity_scope = redis.call('HGET', KEYS[1], 'user_id') or ''
if string.sub(identity_scope, 1, 4) ~= 'rt1:' then identity_scope = '' end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return -2
end
local old = redis.call('HGET', KEYS[1], 'status') or ''
if old == ARGV[3] then return 0 end
if old == 'completed' or old == 'failed' or old == 'cancelled' or old == 'timed_out' then
  return -3
end
if ARGV[3] ~= 'completed' and ARGV[3] ~= 'failed'
   and ARGV[3] ~= 'cancelled' and ARGV[3] ~= 'timed_out' then return -4 end
redis.call('HSET', KEYS[1],
  'status', ARGV[3], 'finished_at', ARGV[4], 'result_json', ARGV[5],
  'error_code', ARGV[6], 'error', ARGV[7], 'http_status', ARGV[8],
  'lease_until_ms', '0',
  'state_version', tonumber(redis.call('HGET', KEYS[1], 'state_version') or '0') + 1)
redis.call('ZREM', KEYS[4], KEYS[1])
redis.call('ZREM', KEYS[5], KEYS[1])
redis.call('HINCRBY', KEYS[6], old, -1)
redis.call('HINCRBY', KEYS[6], ARGV[3], 1)
redis.call('XADD', KEYS[2], '*',
  'type', 'status', 'status', ARGV[3], 'timestamp', ARGV[4])
redis.call('XADD', KEYS[3], '*',
  'event_id', ARGV[10], 'schema_version', '1',
  'event_type', 'Run' .. string.upper(string.sub(ARGV[3], 1, 1)) .. string.sub(ARGV[3], 2),
  'request_id', redis.call('HGET', KEYS[1], 'request_id'),
  'agent_id', redis.call('HGET', KEYS[1], 'agent_id'),
  'thread_id', redis.call('HGET', KEYS[1], 'thread_id'),
  'identity_scope', identity_scope,
  'status', ARGV[3], 'timestamp', ARGV[4],
  'owner_id', ARGV[1], 'fencing_token', ARGV[2],
  'error_code', ARGV[6], 'detail_digest', ARGV[11])
for index = 1, 8 do
  if index ~= 3 and index ~= 4 and index ~= 5 and index ~= 6 then
    redis.call('PEXPIRE', KEYS[index], tonumber(ARGV[9]))
  end
end
return 1
"""


# KEYS: state, events, lifecycle, audit, active, running, metrics, dedupe
# ARGV: user-id, timestamp, retention-ms, audit-id, error-payload, done-payload,
#       maxlen
_CANCEL_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, 0} end
local stored_user = redis.call('HGET', KEYS[1], 'user_id') or ''
local identity_scope = stored_user
if string.sub(identity_scope, 1, 4) ~= 'rt1:' then identity_scope = '' end
if stored_user ~= '' and stored_user ~= ARGV[1] then return {-2, 0} end
local old = redis.call('HGET', KEYS[1], 'status') or ''
if old == 'completed' or old == 'failed' or old == 'cancelled' or old == 'timed_out' then
  return {0, tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0')}
end
local fence = redis.call('HINCRBY', KEYS[1], 'fencing_token', 1)
redis.call('HSET', KEYS[1],
  'status', 'cancelled', 'finished_at', ARGV[2],
  'error_code', 'run_cancelled', 'error', 'Run was cancelled.',
  'http_status', '409', 'lease_until_ms', '0',
  'state_version', tonumber(redis.call('HGET', KEYS[1], 'state_version') or '0') + 1)
if redis.call('HGET', KEYS[1], 'kind') == 'stream' then
  local event_id = redis.call('HINCRBY', KEYS[1], 'last_event_id', 1)
  redis.call('XADD', KEYS[2], event_id .. '-0',
    'payload', ARGV[5], 'fencing_token', fence, 'terminal', '0')
  local done_id = redis.call('HINCRBY', KEYS[1], 'last_event_id', 1)
  redis.call('XADD', KEYS[2], done_id .. '-0',
    'payload', ARGV[6], 'fencing_token', fence, 'terminal', '1')
  redis.call('HSET', KEYS[1], 'terminal_event_emitted', '1')
  redis.call('XTRIM', KEYS[2], 'MAXLEN', tonumber(ARGV[7]))
end
redis.call('ZREM', KEYS[5], KEYS[1])
redis.call('ZREM', KEYS[6], KEYS[1])
redis.call('HINCRBY', KEYS[7], old, -1)
redis.call('HINCRBY', KEYS[7], 'cancelled', 1)
redis.call('XADD', KEYS[3], '*',
  'type', 'status', 'status', 'cancelled', 'timestamp', ARGV[2])
redis.call('XADD', KEYS[4], '*',
  'event_id', ARGV[4], 'schema_version', '1', 'event_type', 'RunCancelled',
  'request_id', redis.call('HGET', KEYS[1], 'request_id'),
  'agent_id', redis.call('HGET', KEYS[1], 'agent_id'),
  'thread_id', redis.call('HGET', KEYS[1], 'thread_id'),
  'identity_scope', identity_scope,
  'status', 'cancelled', 'timestamp', ARGV[2],
  'owner_id', redis.call('HGET', KEYS[1], 'owner_id'), 'fencing_token', fence,
  'error_code', 'run_cancelled')
for index = 1, 8 do
  if index ~= 4 and index ~= 5 and index ~= 6 and index ~= 7 then
    redis.call('PEXPIRE', KEYS[index], tonumber(ARGV[3]))
  end
end
return {1, fence}
"""


# KEYS: state, audit
# ARGV: owner, fence, run-id, timestamp, audit-id
_SET_RUN_ID_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local identity_scope = redis.call('HGET', KEYS[1], 'user_id') or ''
if string.sub(identity_scope, 1, 4) ~= 'rt1:' then identity_scope = '' end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return -2
end
redis.call('HSET', KEYS[1], 'run_id', ARGV[3])
redis.call('XADD', KEYS[2], '*',
  'event_id', ARGV[5], 'schema_version', '1', 'event_type', 'RunIdAssigned',
  'request_id', redis.call('HGET', KEYS[1], 'request_id'),
  'agent_id', redis.call('HGET', KEYS[1], 'agent_id'),
  'thread_id', redis.call('HGET', KEYS[1], 'thread_id'),
  'identity_scope', identity_scope,
  'status', redis.call('HGET', KEYS[1], 'status'), 'timestamp', ARGV[4],
  'owner_id', ARGV[1], 'fencing_token', ARGV[2])
return 1
"""


# KEYS: state, audit
# ARGV: owner, fence, code, message, http status, timestamp, audit-id, digest
_MARK_STREAM_FAILED_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local identity_scope = redis.call('HGET', KEYS[1], 'user_id') or ''
if string.sub(identity_scope, 1, 4) ~= 'rt1:' then identity_scope = '' end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return -2
end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status == 'completed' or status == 'failed' or status == 'cancelled' or status == 'timed_out' then
  return -3
end
redis.call('HSET', KEYS[1],
  'stream_failed', '1', 'error_code', ARGV[3],
  'error', ARGV[4], 'http_status', ARGV[5])
redis.call('XADD', KEYS[2], '*',
  'event_id', ARGV[7], 'schema_version', '1', 'event_type', 'RunStreamFailed',
  'request_id', redis.call('HGET', KEYS[1], 'request_id'),
  'agent_id', redis.call('HGET', KEYS[1], 'agent_id'),
  'thread_id', redis.call('HGET', KEYS[1], 'thread_id'),
  'identity_scope', identity_scope,
  'status', status, 'timestamp', ARGV[6], 'owner_id', ARGV[1],
  'fencing_token', ARGV[2], 'error_code', ARGV[3], 'detail_digest', ARGV[8])
return 1
"""


# KEYS: state, events, event-dedupe, lifecycle, audit, active, running, metrics
# ARGV: owner, fence, interaction-id, interaction-version, payload, max-events,
#       event-key, timestamp, audit-id, stream-audit-payload
_PAUSE_FOR_INPUT_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, 0} end
if redis.call('HGET', KEYS[1], 'owner_id') ~= ARGV[1]
   or tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0') ~= tonumber(ARGV[2]) then
  return {-2, 0}
end
local old = redis.call('HGET', KEYS[1], 'status') or ''
if old == 'waiting_for_input' then
  if redis.call('HGET', KEYS[1], 'interaction_id') == ARGV[3]
     and tonumber(redis.call('HGET', KEYS[1], 'interaction_state_version') or '-1') == tonumber(ARGV[4]) then
    return {0, tonumber(redis.call('HGET', KEYS[3], ARGV[7]) or '0')}
  end
  return {-4, 0}
end
if old ~= 'running' then return {-3, 0} end
local event_id = redis.call('HINCRBY', KEYS[1], 'last_event_id', 1)
redis.call('XADD', KEYS[2], event_id .. '-0', 'payload', ARGV[5])
redis.call('XTRIM', KEYS[2], 'MAXLEN', tonumber(ARGV[6]))
redis.call('HSET', KEYS[3], ARGV[7], event_id)
redis.call('HSET', KEYS[1],
  'status', 'waiting_for_input', 'interaction_id', ARGV[3],
  'interaction_state_version', ARGV[4], 'lease_until_ms', '0',
  'finished_at', '',
  'state_version', tonumber(redis.call('HGET', KEYS[1], 'state_version') or '0') + 1)
redis.call('ZREM', KEYS[6], KEYS[1])
redis.call('ZREM', KEYS[7], KEYS[1])
redis.call('HINCRBY', KEYS[8], 'running', -1)
redis.call('HINCRBY', KEYS[8], 'waiting_for_input', 1)
redis.call('XADD', KEYS[4], '*', 'type', 'status', 'status', 'waiting_for_input',
  'interaction_id', ARGV[3], 'state_version', ARGV[4], 'timestamp', ARGV[8])
redis.call('XADD', KEYS[5], '*', 'event_id', ARGV[9], 'schema_version', '1',
  'event_type', 'RunWaitingForInput',
  'request_id', redis.call('HGET', KEYS[1], 'request_id'),
  'agent_id', redis.call('HGET', KEYS[1], 'agent_id'),
  'thread_id', redis.call('HGET', KEYS[1], 'thread_id'),
  'status', 'waiting_for_input', 'timestamp', ARGV[8],
  'owner_id', ARGV[1], 'fencing_token', ARGV[2])
if ARGV[10] ~= '' then redis.call('XADD', KEYS[5], '*', 'payload', ARGV[10]) end
return {1, event_id}
"""


# KEYS: state, lifecycle, audit, active, running, metrics, response-dedupe
# ARGV: user-id, interaction-id, response-request-id, interaction-version,
#       new-owner, lease-ms, timestamp, audit-id
_RESUME_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, 0} end
local stored_user = redis.call('HGET', KEYS[1], 'user_id') or ''
if stored_user ~= '' and stored_user ~= ARGV[1] then return {-2, 0} end
local response_key = 'response:' .. ARGV[3]
local prior = redis.call('HGET', KEYS[7], response_key)
local response_identity = ARGV[2] .. ':' .. ARGV[4]
if prior then
  if prior == response_identity then
    return {0, tonumber(redis.call('HGET', KEYS[1], 'fencing_token') or '0')}
  end
  return {-5, 0}
end
if redis.call('HGET', KEYS[1], 'status') ~= 'waiting_for_input' then return {-3, 0} end
if redis.call('HGET', KEYS[1], 'interaction_id') ~= ARGV[2]
   or tonumber(redis.call('HGET', KEYS[1], 'interaction_state_version') or '-1') ~= tonumber(ARGV[4]) then
  return {-4, 0}
end
local clock = redis.call('TIME')
local now_ms = (tonumber(clock[1]) * 1000) + math.floor(tonumber(clock[2]) / 1000)
local fence = redis.call('HINCRBY', KEYS[1], 'fencing_token', 1)
local lease_until = now_ms + tonumber(ARGV[6])
redis.call('HSET', KEYS[1],
  'status', 'queued', 'owner_id', ARGV[5], 'lease_until_ms', lease_until,
  'interaction_id', '', 'interaction_state_version', '', 'finished_at', '',
  'error_code', '', 'error', '', 'terminal_event_emitted', '0',
  'state_version', tonumber(redis.call('HGET', KEYS[1], 'state_version') or '0') + 1)
redis.call('HSET', KEYS[7], response_key, response_identity)
redis.call('ZADD', KEYS[4], lease_until, KEYS[1])
redis.call('ZREM', KEYS[5], KEYS[1])
redis.call('HINCRBY', KEYS[6], 'waiting_for_input', -1)
redis.call('HINCRBY', KEYS[6], 'queued', 1)
redis.call('XADD', KEYS[2], '*', 'type', 'status', 'status', 'queued',
  'interaction_id', ARGV[2], 'state_version', ARGV[4], 'timestamp', ARGV[7])
redis.call('XADD', KEYS[3], '*', 'event_id', ARGV[8], 'schema_version', '1',
  'event_type', 'RunInputAccepted',
  'request_id', redis.call('HGET', KEYS[1], 'request_id'),
  'agent_id', redis.call('HGET', KEYS[1], 'agent_id'),
  'thread_id', redis.call('HGET', KEYS[1], 'thread_id'),
  'status', 'queued', 'timestamp', ARGV[7],
  'owner_id', ARGV[5], 'fencing_token', fence)
return {1, fence}
"""


class RedisRunRegistry:
    """Cross-instance RunRegistry using Redis hashes, streams, leases and fencing."""

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
        redis_url: str = "redis://localhost:6379/0",
        redis_client: Any | None = None,
        key_prefix: str = "ratsnest",
        instance_id: str | None = None,
        lease_seconds: float = 30.0,
        stream_block_ms: int = 5_000,
        audit_outbox_maxlen: int = 100_000,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.max_queued = max_queued
        self.default_timeout = default_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self.event_buffer_size = event_buffer_size
        self.max_event_bytes = max_event_bytes
        self.retention_seconds = retention_seconds
        self.redis_url = redis_url
        self.key_prefix = key_prefix.rstrip(":")
        self.instance_id = instance_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        )
        self.lease_seconds = max(3.0, lease_seconds)
        self.stream_block_ms = max(100, min(stream_block_ms, 60_000))
        # This is an operational alert threshold, not a lossy XTRIM limit. Audit
        # entries are deleted atomically by the relay only after Kafka ACKs them.
        self.audit_outbox_maxlen = max(1_000, audit_outbox_maxlen)
        self._redis = redis_client
        self._owns_redis = redis_client is None
        self._startup_lock = asyncio.Lock()
        self._tasks_guard = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    async def startup(self) -> None:
        """Create and verify the connection; safe to call more than once."""

        if self._redis is not None:
            await self._redis.ping()
            return
        async with self._startup_lock:
            if self._redis is None:
                from redis.asyncio import Redis  # Imported lazily by design.

                self._redis = Redis.from_url(self.redis_url, decode_responses=True)
            await self._redis.ping()

    async def healthcheck(self) -> dict[str, str]:
        client = await self._client()
        await client.ping()
        return {"backend": "redis", "status": "ready"}

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
    ) -> tuple[RunHandle, bool]:
        client = await self._client()
        keys = self._keys(request_id)
        effective_timeout = min(
            timeout_seconds or self.default_timeout,
            self.default_timeout,
        )
        reply = await client.eval(
            _CREATE_OR_GET_LUA,
            6,
            keys["state"],
            keys["lifecycle"],
            keys["audit"],
            keys["active"],
            keys["running"],
            keys["metrics"],
            request_id,
            fingerprint,
            kind,
            agent_id,
            thread_id,
            user_id or "",
            effective_timeout,
            self.event_buffer_size,
            self.max_event_bytes,
            self.instance_id,
            self._lease_ms,
            _iso_now(),
            self.max_concurrent + self.max_queued,
            uuid.uuid4().hex,
        )
        code = int(reply[0])
        if code == -1:
            raise RunConflictError("request_id was already used for a different request.")
        if code == -2:
            raise RunAccessError(request_id)
        if code == -3:
            raise RunOverloadedError(
                "The agent run queue is full; retry after an active run completes."
            )

        handle = await self._load_handle(request_id)
        owns_execution = code in {1, 2}
        if owns_execution:
            async with self._tasks_guard:
                task = asyncio.create_task(
                    self._execute(handle, producer),
                    name=f"redis-agent-run:{request_id}",
                )
                self._tasks[request_id] = task
                handle.task = task
        return handle, owns_execution

    async def append_event(
        self,
        record: RunHandle,
        payload: str,
        *,
        event_key: str | None = None,
    ) -> int:
        encoded_size = len(payload.encode("utf-8"))
        if encoded_size > self.max_event_bytes:
            await self.mark_stream_failed(
                record,
                code="event_too_large",
                message=f"Stream event exceeded the {self.max_event_bytes}-byte service limit.",
            )
            payload = self._error_payload(
                record.error or "Stream event exceeded the service limit.",
                "event_too_large",
                retryable=False,
            )

        keys = self._keys(record.request_id)
        client = await self._client()
        audit_payload = self._stream_audit_payload(record, payload)
        reply = await client.eval(
            _APPEND_EVENT_LUA,
            4,
            keys["state"],
            keys["events"],
            keys["dedupe"],
            keys["audit"],
            self.instance_id,
            record.fencing_token,
            payload,
            record.event_buffer_size,
            event_key or "",
            "1" if _is_done_payload(payload) else "0",
            audit_payload,
        )
        code, event_id = int(reply[0]), int(reply[1])
        if code == -1:
            raise RunNotFoundError(record.request_id)
        if code == -2:
            raise RunConflictError("Run ownership changed; stale writer was fenced.")
        if code == -3:
            raise RunConflictError("Cannot append an event to a terminal run.")
        if code == -4:
            raise InvalidRunTransitionError(
                "A run waiting for input cannot emit a terminal stream marker."
            )
        record.next_event_id = max(record.next_event_id, event_id + 1)
        record.newest_event_id = event_id
        record.oldest_event_id = record.oldest_event_id or event_id
        if _is_done_payload(payload):
            record.terminal_event_emitted = True
        return event_id

    async def pause_for_input(
        self,
        record: RunHandle,
        *,
        interaction_id: str,
        state_version: int,
        payload: str,
    ) -> RunHandle:
        if len(payload.encode("utf-8")) > self.max_event_bytes:
            raise InvalidRunTransitionError(
                f"Human-input event exceeded the {self.max_event_bytes}-byte service limit."
            )
        keys = self._keys(record.request_id)
        client = await self._client()
        event_key = f"interaction:{interaction_id}:{state_version}"
        audit_payload = self._stream_audit_payload(record, payload)
        reply = await client.eval(
            _PAUSE_FOR_INPUT_LUA,
            8,
            keys["state"],
            keys["events"],
            keys["dedupe"],
            keys["lifecycle"],
            keys["audit"],
            keys["active"],
            keys["running"],
            keys["metrics"],
            self.instance_id,
            record.fencing_token,
            interaction_id,
            state_version,
            payload,
            record.event_buffer_size,
            event_key,
            _iso_now(),
            uuid.uuid4().hex,
            audit_payload,
        )
        code, event_id = int(reply[0]), int(reply[1])
        if code in {-1, -2}:
            self._raise_owner_write_error(code, record.request_id)
        if code == -3:
            current = await self._load_handle(record.request_id)
            raise InvalidRunTransitionError(
                f"Cannot pause a run in state {current.status!r}."
            )
        if code == -4:
            raise InteractionConflictError("Run is waiting for a different interaction.")
        record.status = "waiting_for_input"
        record.interaction_id = interaction_id
        record.interaction_state_version = state_version
        record.lease_until_ms = 0
        if event_id:
            record.next_event_id = max(record.next_event_id, event_id + 1)
            record.newest_event_id = event_id
            record.oldest_event_id = record.oldest_event_id or event_id
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
    ) -> tuple[RunHandle, bool]:
        keys = self._keys(request_id)
        client = await self._client()
        reply = await client.eval(
            _RESUME_LUA,
            7,
            keys["state"],
            keys["lifecycle"],
            keys["audit"],
            keys["active"],
            keys["running"],
            keys["metrics"],
            keys["dedupe"],
            user_id or "",
            interaction_id,
            response_request_id,
            state_version,
            self.instance_id,
            self._lease_ms,
            _iso_now(),
            uuid.uuid4().hex,
        )
        code, fence = int(reply[0]), int(reply[1])
        if code == -1:
            raise RunNotFoundError(request_id)
        if code == -2:
            raise RunAccessError(request_id)
        if code == -3:
            current = await self._load_handle(request_id)
            raise InvalidRunTransitionError(
                f"Cannot resume a run in state {current.status!r}."
            )
        if code in {-4, -5}:
            raise InteractionConflictError("Interaction response conflicts with durable state.")

        handle = await self._load_handle(request_id)
        if code == 0:
            return handle, False
        handle.fencing_token = fence
        async with self._tasks_guard:
            task = asyncio.create_task(
                self._execute(handle, producer),
                name=f"redis-agent-run-resume:{request_id}",
            )
            self._tasks[request_id] = task
            handle.task = task
        return handle, True

    async def subscribe(
        self,
        record: RunHandle,
        *,
        last_event_id: int,
    ) -> AsyncGenerator[str, None]:
        """Replay and follow an SSE stream from any service instance."""

        client = await self._client()
        keys = self._keys(record.request_id)
        cursor = max(0, last_event_id)

        newest_event_id = int(await client.hget(keys["state"], "last_event_id") or 0)
        if cursor > newest_event_id:
            yield self._format_event(
                cursor + 1,
                self._error_payload(
                    "The requested replay position is ahead of the current run; reload thread history.",
                    "replay_gap",
                    retryable=False,
                ),
            )
            return

        gap = await self._replay_gap(keys["state"], keys["events"], cursor)
        if gap is not None:
            yield self._format_event(
                gap - 1,
                self._error_payload(
                    "The requested replay position is no longer buffered; reload thread history.",
                    "replay_gap",
                    retryable=False,
                ),
            )
            return

        block_ms = min(
            self.stream_block_ms,
            max(1, int(self.heartbeat_seconds * 1000)),
        )
        while True:
            values = _decoded_hash(await client.hgetall(keys["state"]))
            if not values:
                raise RunNotFoundError(record.request_id)
            terminal = values.get("status") in _TERMINAL_STATES
            waiting = values.get("status") == "waiting_for_input"
            newest = int(values.get("last_event_id", "0"))
            if (terminal or waiting) and cursor >= newest:
                return

            response = await self._xread_or_idle(
                client,
                {keys["events"]: f"{cursor}-0"},
                count=256,
                block=block_ms,
            )
            if response:
                for _, entries in response:
                    delivery_state = _decoded_hash(await client.hgetall(keys["state"]))
                    if not delivery_state:
                        raise RunNotFoundError(record.request_id)
                    delivery_status = delivery_state.get("status", "")
                    delivery_fence = int(delivery_state.get("fencing_token", "0"))
                    delivery_newest = int(delivery_state.get("last_event_id", "0"))
                    wait_for_terminal = False
                    for stream_id, fields in entries:
                        event_id = self._stream_sequence(stream_id)
                        if event_id <= cursor:
                            continue
                        decoded = _decoded_hash(fields)
                        action = _done_delivery_action(
                            decoded,
                            status=delivery_status,
                            current_fence=delivery_fence,
                            newest_event_id=delivery_newest,
                            event_id=event_id,
                        )
                        if action == "wait":
                            wait_for_terminal = True
                            break
                        cursor = event_id
                        if action == "skip":
                            continue
                        payload = decoded.get("payload", "")
                        yield self._format_event(event_id, payload)
                    if wait_for_terminal:
                        await asyncio.sleep(min(0.05, self.heartbeat_seconds))
                        break
            else:
                gap = await self._replay_gap(keys["state"], keys["events"], cursor)
                if gap is not None:
                    yield self._format_event(
                        gap - 1,
                        self._error_payload(
                            "The requested replay position is no longer buffered; reload thread history.",
                            "replay_gap",
                            retryable=False,
                        ),
                    )
                    return
                yield ": heartbeat\n\n"

            values = _decoded_hash(await client.hgetall(keys["state"]))
            if not values:
                raise RunNotFoundError(record.request_id)
            terminal = values.get("status") in _TERMINAL_STATES
            waiting = values.get("status") == "waiting_for_input"
            newest = int(values.get("last_event_id", "0"))
            if (terminal or waiting) and cursor >= newest:
                return

    async def get(self, request_id: str, user_id: str | None) -> RunHandle:
        handle = await self._load_handle(request_id)
        if handle.user_id and handle.user_id != user_id:
            raise RunAccessError(request_id)
        return handle

    async def cancel(self, request_id: str, user_id: str | None) -> RunHandle:
        handle = await self.get(request_id, user_id)
        if handle.is_terminal:
            return handle
        keys = self._keys(request_id)
        client = await self._client()
        reply = await client.eval(
            _CANCEL_LUA,
            8,
            keys["state"],
            keys["events"],
            keys["lifecycle"],
            keys["audit"],
            keys["active"],
            keys["running"],
            keys["metrics"],
            keys["dedupe"],
            user_id or "",
            _iso_now(),
            self._retention_ms,
            uuid.uuid4().hex,
            self._error_payload("Run was cancelled.", "run_cancelled", retryable=False),
            "data: [DONE]\n\n",
            handle.event_buffer_size,
        )
        code = int(reply[0])
        if code == -1:
            raise RunNotFoundError(request_id)
        if code == -2:
            raise RunAccessError(request_id)

        async with self._tasks_guard:
            local_task = self._tasks.get(request_id)
        if local_task is not None and not local_task.done():
            local_task.cancel()
        return await self._load_handle(request_id)

    async def wait_terminal(
        self,
        record: RunHandle,
        *,
        timeout_seconds: float | None = None,
    ) -> RunHandle:
        """Wait durably for a terminal state, including runs owned elsewhere."""

        async def wait_loop() -> RunHandle:
            client = await self._client()
            key = self._keys(record.request_id)["lifecycle"]
            latest = await client.xrevrange(key, max="+", min="-", count=1)
            cursor = _text(latest[0][0]) if latest else "0-0"
            while True:
                current = await self.get(record.request_id, record.user_id)
                if current.is_terminal:
                    current.done.set()
                    return current
                response = await self._xread_or_idle(
                    client,
                    {key: cursor},
                    count=32,
                    block=min(
                        self.stream_block_ms,
                        max(1, int(self.heartbeat_seconds * 1000)),
                    ),
                )
                if response:
                    for _, entries in response:
                        if entries:
                            cursor = _text(entries[-1][0])

        if timeout_seconds is None:
            return await wait_loop()
        async with asyncio.timeout(timeout_seconds):
            return await wait_loop()

    async def set_run_id(self, record: RunHandle, run_id: str | None) -> RunHandle:
        if not run_id or record.run_id:
            return record
        keys = self._keys(record.request_id)
        client = await self._client()
        code = int(
            await client.eval(
                _SET_RUN_ID_LUA,
                2,
                keys["state"],
                keys["audit"],
                self.instance_id,
                record.fencing_token,
                run_id,
                _iso_now(),
                uuid.uuid4().hex,
            )
        )
        self._raise_owner_write_error(code, record.request_id)
        record.run_id = run_id
        return record

    async def mark_stream_failed(
        self,
        record: RunHandle,
        code: str = "agent_stream_error",
        message: str = "The agent stream reported an error.",
        http_status: int = 500,
    ) -> RunHandle:
        keys = self._keys(record.request_id)
        client = await self._client()
        result = int(
            await client.eval(
                _MARK_STREAM_FAILED_LUA,
                2,
                keys["state"],
                keys["audit"],
                self.instance_id,
                record.fencing_token,
                code,
                message,
                http_status,
                _iso_now(),
                uuid.uuid4().hex,
                _digest(message),
            )
        )
        self._raise_owner_write_error(result, record.request_id)
        record.stream_failed = True
        record.error_code = code
        record.error = message
        record.http_status = http_status
        return record

    async def shutdown(self) -> None:
        self._closing = True
        async with self._tasks_guard:
            tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._redis is not None and self._owns_redis:
            await self._redis.aclose()
        self._redis = None

    async def metrics(self) -> dict[str, int]:
        client = await self._client()
        values = _decoded_hash(await client.hgetall(self._global_key("metrics")))
        clock = await client.time()
        now_ms = int(clock[0]) * 1000 + int(clock[1]) // 1000
        active = int(await client.zcount(self._global_key("active"), f"({now_ms}", "+inf"))
        running = int(await client.zcount(self._global_key("running"), f"({now_ms}", "+inf"))
        audit_outbox_length = int(await client.xlen(self._global_key("audit-outbox")))
        states = {
            state: max(0, int(values.get(state, "0")))
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
        states["running"] = running
        states["queued"] = max(0, active - running)
        return {
            **states,
            "retained": max(0, int(values.get("retained", "0"))),
            "capacity": self.max_concurrent,
            "queue_capacity": self.max_queued,
            "audit_outbox_length": audit_outbox_length,
            "audit_outbox_alert_length": self.audit_outbox_maxlen,
        }

    async def _execute(self, record: RunHandle, producer: Producer) -> None:
        current_task = asyncio.current_task()
        assert current_task is not None
        lease_task = asyncio.create_task(
            self._lease_loop(record, current_task),
            name=f"redis-run-lease:{record.request_id}",
        )
        cancellation_task = asyncio.create_task(
            self._cancellation_watch(record, current_task),
            name=f"redis-run-cancel-watch:{record.request_id}",
        )
        try:
            await self._wait_for_running_slot(record)
            async with asyncio.timeout(record.timeout_seconds):
                result = await producer(record)
            refreshed = await self.get(record.request_id, record.user_id)
            if (
                refreshed.fencing_token != record.fencing_token
                or refreshed.owner_id != self.instance_id
                or refreshed.status == "waiting_for_input"
            ):
                return
            if record.kind == "stream" and not refreshed.terminal_event_emitted:
                await self.append_event(
                    record,
                    "data: [DONE]\n\n",
                    event_key=f"terminal-done:{record.fencing_token}",
                )
                refreshed = await self.get(record.request_id, record.user_id)
            target: RunState = "failed" if refreshed.stream_failed else "completed"
            await self._transition(
                record,
                target,
                result=result,
                error_code=refreshed.error_code,
                error=refreshed.error,
                http_status=refreshed.http_status,
            )
        except TimeoutError:
            current = await self._load_handle(record.request_id)
            if (
                current.fencing_token != record.fencing_token
                or current.owner_id != self.instance_id
                or current.status == "waiting_for_input"
            ):
                return
            await self._append_terminal_error(
                record,
                "Run exceeded its configured timeout.",
                "run_timeout",
                retryable=True,
            )
            await self._transition(
                record,
                "timed_out",
                error_code="run_timeout",
                error=f"Run exceeded {record.timeout_seconds:g} seconds.",
                http_status=504,
            )
        except asyncio.CancelledError:
            if self._closing:
                # SIGTERM/redeployment must not turn a durable run into a user
                # cancellation. Leave its state intact and make it immediately
                # eligible for a fenced takeover by the replacement instance.
                await self._release_execution_lease(record)
            else:
                current = await self._load_handle(record.request_id)
                if (
                    current.fencing_token != record.fencing_token
                    or current.owner_id != self.instance_id
                    or current.status == "waiting_for_input"
                ):
                    return
                if not current.is_terminal:
                    await self._append_terminal_error(
                        record,
                        "Run was cancelled.",
                        "run_cancelled",
                        retryable=False,
                    )
                    await self._transition(
                        record,
                        "cancelled",
                        error_code="run_cancelled",
                        error="Run was cancelled.",
                        http_status=409,
                    )
        except HTTPException as exc:
            current = await self._load_handle(record.request_id)
            if (
                current.fencing_token != record.fencing_token
                or current.owner_id != self.instance_id
                or current.status == "waiting_for_input"
            ):
                return
            await self._append_terminal_error(
                record,
                str(exc.detail),
                "request_rejected",
                retryable=False,
            )
            await self._transition(
                record,
                "failed",
                error_code="request_rejected",
                error=str(exc.detail),
                http_status=exc.status_code,
            )
        except Exception:
            current = await self._load_handle(record.request_id)
            if (
                current.fencing_token != record.fencing_token
                or current.owner_id != self.instance_id
                or current.status == "waiting_for_input"
            ):
                return
            await self._append_terminal_error(
                record,
                "Internal server error",
                "internal_error",
                retryable=False,
            )
            await self._transition(
                record,
                "failed",
                error_code="internal_error",
                error="Internal server error",
                http_status=500,
            )
        finally:
            for helper in (lease_task, cancellation_task):
                helper.cancel()
            await asyncio.gather(lease_task, cancellation_task, return_exceptions=True)
            try:
                current = await self._load_handle(record.request_id)
            except RunNotFoundError:
                current = None
            if current is not None and current.is_terminal:
                record.done.set()
            async with self._tasks_guard:
                if self._tasks.get(record.request_id) is current_task:
                    self._tasks.pop(record.request_id, None)

    async def _wait_for_running_slot(self, record: RunHandle) -> None:
        keys = self._keys(record.request_id)
        client = await self._client()
        while True:
            code = int(
                await client.eval(
                    _ACQUIRE_RUNNING_LUA,
                    6,
                    keys["state"],
                    keys["lifecycle"],
                    keys["audit"],
                    keys["active"],
                    keys["running"],
                    keys["metrics"],
                    self.instance_id,
                    record.fencing_token,
                    self.max_concurrent,
                    self._lease_ms,
                    _iso_now(),
                    uuid.uuid4().hex,
                )
            )
            if code == 1:
                record.status = "running"
                record.started_at = _utcnow()
                return
            if code in {-1, -2, -3}:
                current = await self._load_handle(record.request_id)
                if current.is_terminal:
                    raise asyncio.CancelledError
                raise RunConflictError("Run ownership changed before execution started.")
            await asyncio.sleep(min(0.25, self.lease_seconds / 10))

    async def _lease_loop(
        self,
        record: RunHandle,
        owner_task: asyncio.Task[None],
    ) -> None:
        keys = self._keys(record.request_id)
        client = await self._client()
        while not owner_task.done():
            await asyncio.sleep(self.lease_seconds / 3)
            current = await self._load_handle(record.request_id)
            if current.status == "waiting_for_input":
                return
            try:
                renewed = int(
                    await client.eval(
                        _HEARTBEAT_LUA,
                        3,
                        keys["state"],
                        keys["active"],
                        keys["running"],
                        self.instance_id,
                        record.fencing_token,
                        self._lease_ms,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Without a confirmed renewal this process can no longer prove
                # ownership. Stop its producer before another instance can
                # acquire the expired lease and create a split-brain writer.
                owner_task.cancel()
                return
            if renewed != 1:
                owner_task.cancel()
                return

    async def _release_execution_lease(self, record: RunHandle) -> None:
        client = await self._client()
        keys = self._keys(record.request_id)
        await client.eval(
            _RELEASE_LEASE_LUA,
            3,
            keys["state"],
            keys["active"],
            keys["running"],
            self.instance_id,
            record.fencing_token,
        )

    async def _cancellation_watch(
        self,
        record: RunHandle,
        owner_task: asyncio.Task[None],
    ) -> None:
        client = await self._client()
        keys = self._keys(record.request_id)
        latest = await client.xrevrange(keys["lifecycle"], max="+", min="-", count=1)
        cursor = _text(latest[0][0]) if latest else "0-0"
        while not owner_task.done():
            current = await self._load_handle(record.request_id)
            if current.status == "waiting_for_input":
                return
            if current.is_terminal:
                owner_task.cancel()
                return
            response = await self._xread_or_idle(
                client,
                {keys["lifecycle"]: cursor},
                count=16,
                block=1000,
            )
            if response:
                for _, entries in response:
                    if entries:
                        cursor = _text(entries[-1][0])

    async def _transition(
        self,
        record: RunHandle,
        status: RunState,
        *,
        result: Any = None,
        error_code: str | None = None,
        error: str | None = None,
        http_status: int = 500,
    ) -> None:
        keys = self._keys(record.request_id)
        client = await self._client()
        result_json = "" if result is None else _json_dumps(result)
        detail_digest = _digest(result_json or error or "")
        code = int(
            await client.eval(
                _TRANSITION_LUA,
                8,
                keys["state"],
                keys["lifecycle"],
                keys["audit"],
                keys["active"],
                keys["running"],
                keys["metrics"],
                keys["events"],
                keys["dedupe"],
                self.instance_id,
                record.fencing_token,
                status,
                _iso_now(),
                result_json,
                error_code or "",
                error or "",
                http_status,
                self._retention_ms,
                uuid.uuid4().hex,
                detail_digest,
            )
        )
        if code in {-1, -2}:
            self._raise_owner_write_error(code, record.request_id)
        if code in {-3, -4}:
            current = await self._load_handle(record.request_id)
            if current.status != status:
                raise RunConflictError(
                    f"Illegal run transition from {current.status!r} to {status!r}."
                )
        record.status = status
        record.result = result
        record.error_code = error_code
        record.error = error
        record.http_status = http_status
        record.finished_at = _utcnow()

    async def _append_terminal_error(
        self,
        record: RunHandle,
        message: str,
        code: str,
        *,
        retryable: bool,
    ) -> None:
        if record.kind != "stream":
            return
        try:
            await self.append_event(
                record,
                self._error_payload(message, code, retryable=retryable),
                event_key=f"terminal-error:{code}",
            )
            if not record.terminal_event_emitted:
                await self.append_event(
                    record,
                    "data: [DONE]\n\n",
                    event_key=f"terminal-done:{record.fencing_token}",
                )
        except (RunConflictError, RunNotFoundError):
            # A concurrent cancellation may already have emitted terminal events.
            return

    async def _load_handle(self, request_id: str) -> RunHandle:
        client = await self._client()
        keys = self._keys(request_id)
        raw_state, raw_rows, clock, raw_oldest = await client.eval(
            _LOAD_STATUS_SNAPSHOT_LUA,
            2,
            keys["state"],
            keys["events"],
        )
        values = _decode_flat_pairs(list(raw_state))
        if not values:
            raise RunNotFoundError(request_id)
        result: Any = None
        if values.get("result_json"):
            try:
                result = json.loads(values["result_json"])
            except json.JSONDecodeError:
                result = None
        event_rows = _decode_snapshot_rows(list(raw_rows))
        event_rows.sort(key=lambda row: self._stream_sequence(row[0]))
        oldest_rows = _decode_snapshot_rows(list(raw_oldest))
        last_event_id = int(values.get("last_event_id", "0"))
        checked_at = datetime.fromtimestamp(
            (int(clock[0]) * 1000 + int(clock[1]) // 1000) / 1000,
            UTC,
        )
        lease_until_ms = int(values.get("lease_until_ms", "0"))
        execution_pending = values.get("status") in {"queued", "running"}
        result_object = result if isinstance(result, dict) else {}
        ui_snapshot = build_ui_snapshot(
            [
                (self._stream_sequence(stream_id), fields.get("payload", ""))
                for stream_id, fields in event_rows
            ],
            snapshot_cursor=last_event_id,
            run_status=values.get("status", "queued"),
            artifact_manifest=result_object.get("artifact_manifest"),
            delivery_status=result_object.get("delivery_status"),
        )
        handle = RunHandle(
            request_id=values["request_id"],
            fingerprint=values["fingerprint"],
            kind=values["kind"],
            agent_id=values["agent_id"],
            thread_id=values["thread_id"],
            user_id=values.get("user_id") or None,
            timeout_seconds=float(values["timeout_seconds"]),
            event_buffer_size=int(values["event_buffer_size"]),
            status=values.get("status", "queued"),
            run_id=values.get("run_id") or None,
            created_at=_parse_datetime(values.get("created_at")) or _utcnow(),
            started_at=_parse_datetime(values.get("started_at")),
            finished_at=_parse_datetime(values.get("finished_at")),
            result=result,
            error_code=values.get("error_code") or None,
            error=values.get("error") or None,
            interaction_id=values.get("interaction_id") or None,
            interaction_state_version=(
                int(values["interaction_state_version"])
                if values.get("interaction_state_version")
                else None
            ),
            http_status=int(values.get("http_status", "500")),
            stream_failed=values.get("stream_failed") == "1",
            terminal_event_emitted=values.get("terminal_event_emitted") == "1",
            next_event_id=last_event_id + 1,
            oldest_event_id=(
                self._stream_sequence(oldest_rows[0][0]) if oldest_rows else None
            ),
            newest_event_id=(self._stream_sequence(event_rows[-1][0]) if event_rows else None),
            owner_id=values.get("owner_id", ""),
            fencing_token=int(values.get("fencing_token", "0")),
            lease_until_ms=lease_until_ms,
            execution_lease_active=(
                execution_pending
                and lease_until_ms > int(checked_at.timestamp() * 1000)
            ),
            recoverable=(
                execution_pending
                and lease_until_ms <= int(checked_at.timestamp() * 1000)
            ),
            lease_expires_at=(
                datetime.fromtimestamp(lease_until_ms / 1000, UTC)
                if lease_until_ms > 0
                else None
            ),
            checked_at=checked_at,
            ui_snapshot=ui_snapshot,
        )
        if handle.is_terminal:
            handle.done.set()
        async with self._tasks_guard:
            if request_id in self._tasks:
                handle.task = self._tasks[request_id]
        return handle

    async def _replay_gap(
        self,
        state_key: str,
        stream_key: str,
        cursor: int,
    ) -> int | None:
        client = await self._client()
        rows = await client.xrange(stream_key, min="-", max="+", count=1)
        if not rows:
            last_event_id = int(await client.hget(state_key, "last_event_id") or 0)
            return last_event_id + 1 if last_event_id > cursor else None
        oldest = self._stream_sequence(rows[0][0])
        return oldest if cursor < oldest - 1 else None

    async def _client(self) -> Any:
        if self._redis is None:
            await self.startup()
        assert self._redis is not None
        return self._redis

    async def _xread_or_idle(
        self,
        client: Any,
        streams: Mapping[str, str],
        *,
        count: int,
        block: int,
    ) -> Any:
        """Return an empty poll when Redis times out exactly at XREAD's idle boundary."""

        # redis-py 8 defaults socket_timeout to five seconds, which equals this
        # registry's default XREAD block. That client-side idle timeout is not a
        # broken SSE stream; callers re-check durable state after every poll.
        from redis.exceptions import TimeoutError as RedisTimeoutError

        try:
            return await client.xread(streams, count=count, block=block)
        except RedisTimeoutError:
            return []

    def _keys(self, request_id: str) -> dict[str, str]:
        token = _digest(request_id)
        base = f"{self.key_prefix}:{_REGISTRY_SLOT}:run:{token}"
        return {
            "state": base,
            "events": f"{base}:events",
            "lifecycle": f"{base}:lifecycle",
            "dedupe": f"{base}:dedupe",
            "audit": self._global_key("audit-outbox"),
            "active": self._global_key("active"),
            "running": self._global_key("running"),
            "metrics": self._global_key("metrics"),
        }

    def _global_key(self, suffix: str) -> str:
        return f"{self.key_prefix}:{_REGISTRY_SLOT}:{suffix}"

    @property
    def _lease_ms(self) -> int:
        return int(self.lease_seconds * 1000)

    @property
    def _retention_ms(self) -> int:
        return int(self.retention_seconds * 1000)

    @staticmethod
    def _stream_sequence(stream_id: Any) -> int:
        return int(_text(stream_id).split("-", 1)[0])

    @staticmethod
    def _raise_owner_write_error(code: int, request_id: str) -> None:
        if code == -1:
            raise RunNotFoundError(request_id)
        if code == -2:
            raise RunConflictError("Run ownership changed; stale writer was fenced.")
        if code == -3:
            raise RunConflictError("Run is already terminal.")

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
        return (
            "data: "
            + json.dumps(
                {
                    "type": "error",
                    "content": message,
                    "code": code,
                    "retryable": retryable,
                },
                ensure_ascii=False,
            )
            + "\n\n"
        )

    @staticmethod
    def _stream_audit_payload(record: RunHandle, payload: str) -> str:
        """Return safe structured metadata for Kafka, never token or message text."""

        data_lines = [line[6:] for line in payload.splitlines() if line.startswith("data: ")]
        if not data_lines:
            return ""
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            envelope: dict[str, Any] = {"type": "done"}
        else:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return ""
            if not isinstance(parsed, dict) or parsed.get("type") == "token":
                return ""
            envelope = parsed

        stream_type = str(envelope.get("type", "unknown"))[:100]
        event_type = {
            "done": "AgentStreamCompleted",
            "error": "AgentStreamError",
            "message": "AgentMessageEmitted",
        }.get(stream_type, "AgentStreamEvent")
        metadata: dict[str, Any] = {
            "agent_id": record.agent_id,
            "thread_id": record.thread_id,
            "stream_type": stream_type,
        }
        tenant_scope, project_scope = audit_scopes(record.user_id)
        if project_scope is not None:
            metadata["project_scope"] = project_scope
        outcome: str | None = None
        if stream_type == "error":
            code = envelope.get("code")
            if isinstance(code, str):
                metadata["error_code"] = code[:200]
            if isinstance(envelope.get("retryable"), bool):
                metadata["retryable"] = envelope["retryable"]
            outcome = "error"

        content = envelope.get("content")
        if stream_type == "message" and isinstance(content, dict):
            message_type = content.get("type")
            if isinstance(message_type, str):
                metadata["message_type"] = message_type[:100]
            custom = content.get("custom_data")
            if message_type == "custom" and isinstance(custom, dict):
                event_type = "AgentWorkflowMilestone"
                for key in (
                    "event",
                    "kind",
                    "phase",
                    "status",
                    "attempt",
                    "step",
                    "workflow_id",
                    "release_ready",
                    "artifact_count",
                ):
                    value = custom.get(key)
                    if isinstance(value, (str, int, float, bool)):
                        metadata[key] = value[:500] if isinstance(value, str) else value
                status = custom.get("status")
                if isinstance(status, str):
                    outcome = status[:100]

        event = KafkaAuditEvent(
            audit_event_id=uuid.uuid4().hex,
            event_type=event_type,
            source="service.sse",
            outcome=outcome,
            actor_id=_digest(record.user_id) if record.user_id else None,
            tenant_id=tenant_scope,
            request_id=record.request_id,
            resource_type="agent_run",
            resource_id=record.agent_id,
            metadata=metadata,
        )
        return event.model_dump_json()


__all__ = ["RedisRunRegistry", "RunHandle"]
