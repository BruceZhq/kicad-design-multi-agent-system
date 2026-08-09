"""Bounded Redis Stream transport for Temporal LLM output records.

The stream is a live delivery aid, not the audit source. Full records remain in
the per-run JSONL transcript, and Redis failures must never fail EDA work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from service.llm_output import stream_llm_output_record


@dataclass(frozen=True)
class LlmOutputRedisConfig:
    enabled: bool
    url: str | None
    key_prefix: str
    maxlen: int
    ttl_seconds: int
    socket_timeout_seconds: float


@dataclass(frozen=True)
class LlmOutputStreamKeys:
    stream: str
    records: str
    order: str


def llm_output_stream_keys(key_prefix: str, workflow_id: str) -> LlmOutputStreamKeys:
    """Build Redis Cluster-safe keys without exposing the workflow ID."""

    digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    slot = f"{{llm-{digest}}}"
    base = f"{key_prefix}:{slot}:llm-output"
    return LlmOutputStreamKeys(
        stream=f"{base}:stream",
        records=f"{base}:records",
        order=f"{base}:order",
    )


# Exact MAXLEN bounds the live payload. The hash and sorted set provide a
# bounded record_id -> stream-id idempotency window and are trimmed together.
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


def publish_llm_output_best_effort(
    config: LlmOutputRedisConfig,
    *,
    workflow_id: str,
    record: dict[str, Any],
    transcript_path: str | None = None,
    client: Any | None = None,
) -> str | None:
    """Publish idempotently, returning ``None`` on disabled/unavailable Redis."""

    record_id = str(record.get("record_id", "")).strip()
    if not config.enabled or not config.url or not workflow_id or not record_id:
        return None
    keys = llm_output_stream_keys(config.key_prefix, workflow_id)
    payload = json.dumps(
        stream_llm_output_record(record, transcript_path=transcript_path),
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
    except Exception:  # noqa: BLE001 - live output must not stop hardware work
        return None
    finally:
        if owned_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - best-effort transport cleanup
                pass


def decode_llm_stream_response(
    response: list[Any],
) -> tuple[str | None, list[tuple[str, dict[str, Any]]]]:
    """Decode XREAD output while advancing past malformed entries safely."""

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
            if isinstance(record, dict) and record.get("kind") == "llm_output":
                decoded.append((last_id, record))
    return last_id, decoded


class RedisLlmOutputReader:
    """Cursor-based asynchronous reader; one instance is used per wait node."""

    def __init__(self, client: Any, keys: LlmOutputStreamKeys, *, count: int = 128) -> None:
        self._client = client
        self._keys = keys
        self._count = count

    @classmethod
    def connect(
        cls,
        config: LlmOutputRedisConfig,
        workflow_id: str,
    ) -> RedisLlmOutputReader:
        from redis.asyncio import Redis

        client = Redis.from_url(
            config.url,
            socket_connect_timeout=config.socket_timeout_seconds,
            socket_timeout=config.socket_timeout_seconds,
            retry_on_timeout=False,
        )
        return cls(client, llm_output_stream_keys(config.key_prefix, workflow_id))

    async def read_after(
        self, cursor: str
    ) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        response = await self._client.xread(
            {self._keys.stream: cursor or "0-0"},
            count=self._count,
        )
        last_id, records = decode_llm_stream_response(response)
        return last_id or cursor, records

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            await close()
