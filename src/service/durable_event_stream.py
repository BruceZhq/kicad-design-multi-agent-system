"""Reusable, idempotent Redis Stream transport for per-workflow events.

Redis is a bounded delivery bridge.  Callers must retain their own audit copy;
transport loss must never change the underlying engineering outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")


@dataclass(frozen=True)
class RedisEventStreamConfig:
    enabled: bool
    url: str | None
    key_prefix: str
    maxlen: int
    ttl_seconds: int
    socket_timeout_seconds: float


@dataclass(frozen=True)
class RedisEventStreamKeys:
    stream: str
    records: str
    order: str


def event_stream_keys(
    key_prefix: str,
    workflow_id: str,
    *,
    channel: str,
) -> RedisEventStreamKeys:
    """Build Redis Cluster-safe keys without exposing the workflow identity."""

    if not _CHANNEL_RE.fullmatch(channel):
        raise ValueError("event stream channel must be a bounded lowercase slug")
    digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    # Keep the pre-existing LLM stream keys stable across the transport
    # extraction so in-flight workflows can still consume buffered output.
    slot_namespace = "llm" if channel == "llm-output" else "workflow"
    slot = f"{{{slot_namespace}-{digest}}}"
    base = f"{key_prefix}:{slot}:{channel}"
    return RedisEventStreamKeys(
        stream=f"{base}:stream",
        records=f"{base}:records",
        order=f"{base}:order",
    )


# The record hash makes Activity replay idempotent.  The sorted set is trimmed
# with the exact stream bound so the idempotency index cannot grow forever.
_PUBLISH_LUA = r"""
local prior = redis.call('HGET', KEYS[2], ARGV[1])
if prior then return prior end
local stream_id = redis.call(
  'XADD', KEYS[1], 'MAXLEN', '=', tonumber(ARGV[3]), '*', 'payload', ARGV[2])
redis.call('HSET', KEYS[2], ARGV[1], stream_id)
local clock = redis.call('TIME')
local score = (tonumber(clock[1]) * 1000000) + tonumber(clock[2])
redis.call('ZADD', KEYS[3], score, ARGV[1])
local overflow = redis.call('ZCARD', KEYS[3]) - tonumber(ARGV[3])
if overflow > 0 then
  local expired = redis.call('ZRANGE', KEYS[3], 0, overflow - 1)
  if #expired > 0 then
    redis.call('ZREM', KEYS[3], unpack(expired))
    redis.call('HDEL', KEYS[2], unpack(expired))
  end
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[4]))
return stream_id
"""


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def publish_event_best_effort(
    config: RedisEventStreamConfig,
    *,
    workflow_id: str,
    channel: str,
    record: dict[str, Any],
    client: Any | None = None,
) -> str | None:
    """Publish one record once, returning ``None`` when Redis is unavailable."""

    record_id = str(record.get("record_id", "")).strip()
    if not config.enabled or not config.url or not workflow_id or not record_id:
        return None
    keys = event_stream_keys(config.key_prefix, workflow_id, channel=channel)
    payload = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    owned_client = client is None
    try:
        if client is None:
            from redis import Redis

            client = Redis.from_url(
                config.url,
                socket_connect_timeout=config.socket_timeout_seconds,
                socket_timeout=config.socket_timeout_seconds,
                retry_on_timeout=False,
            )
        stream_id = client.eval(
            _PUBLISH_LUA,
            3,
            keys.stream,
            keys.records,
            keys.order,
            record_id,
            payload,
            config.maxlen,
            config.ttl_seconds,
        )
        return _text(stream_id)
    except Exception:  # noqa: BLE001 - telemetry must not stop engineering work
        return None
    finally:
        if owned_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best-effort transport cleanup
                pass


def decode_event_stream_response(
    response: list[Any],
    *,
    expected_kind: str,
) -> tuple[str | None, list[tuple[str, dict[str, Any]]]]:
    """Decode XREAD output and advance past malformed or foreign records."""

    decoded: list[tuple[str, dict[str, Any]]] = []
    last_id: str | None = None
    for _, entries in response or []:
        for stream_id, raw_fields in entries:
            last_id = _text(stream_id)
            fields: Mapping[Any, Any] = raw_fields
            payload = next(
                (_text(value) for key, value in fields.items() if _text(key) == "payload"),
                "",
            )
            try:
                record = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                record = {}
            if isinstance(record, dict) and record.get("kind") == expected_kind:
                decoded.append((last_id, record))
    return last_id, decoded


class RedisEventReader:
    """Cursor-based asynchronous reader for one workflow event channel."""

    def __init__(
        self,
        client: Any,
        keys: RedisEventStreamKeys,
        *,
        expected_kind: str,
        count: int = 128,
    ) -> None:
        self._client = client
        self._keys = keys
        self._expected_kind = expected_kind
        self._count = count

    @classmethod
    def connect(
        cls,
        config: RedisEventStreamConfig,
        workflow_id: str,
        *,
        channel: str,
        expected_kind: str,
    ) -> RedisEventReader:
        from redis.asyncio import Redis

        client = Redis.from_url(
            config.url,
            socket_connect_timeout=config.socket_timeout_seconds,
            socket_timeout=config.socket_timeout_seconds,
            retry_on_timeout=False,
        )
        return cls(
            client,
            event_stream_keys(config.key_prefix, workflow_id, channel=channel),
            expected_kind=expected_kind,
        )

    async def read_after(
        self,
        cursor: str,
    ) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        response = await self._client.xread(
            {self._keys.stream: cursor or "0-0"},
            count=self._count,
        )
        last_id, records = decode_event_stream_response(
            response,
            expected_kind=self._expected_kind,
        )
        return last_id or cursor, records

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            await close()
