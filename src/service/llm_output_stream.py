"""Compatibility API for LLM records on the shared durable event transport."""

from __future__ import annotations

from typing import Any

from service.durable_event_stream import (
    RedisEventReader,
    RedisEventStreamConfig,
    RedisEventStreamKeys,
    decode_event_stream_response,
    event_stream_keys,
    publish_event_best_effort,
)
from service.llm_output import stream_llm_output_record

_LLM_OUTPUT_CHANNEL = "llm-output"
_LLM_OUTPUT_KIND = "llm_output"

# Preserve the public names used by the existing Runtime and worker while the
# Redis implementation itself lives in one generic transport module.
LlmOutputRedisConfig = RedisEventStreamConfig
LlmOutputStreamKeys = RedisEventStreamKeys


def llm_output_stream_keys(key_prefix: str, workflow_id: str) -> LlmOutputStreamKeys:
    return event_stream_keys(
        key_prefix,
        workflow_id,
        channel=_LLM_OUTPUT_CHANNEL,
    )


def publish_llm_output_best_effort(
    config: LlmOutputRedisConfig,
    *,
    workflow_id: str,
    record: dict[str, Any],
    transcript_path: str | None = None,
    client: Any | None = None,
) -> str | None:
    return publish_event_best_effort(
        config,
        workflow_id=workflow_id,
        channel=_LLM_OUTPUT_CHANNEL,
        record=stream_llm_output_record(
            record,
            transcript_path=transcript_path,
        ),
        client=client,
    )


def decode_llm_stream_response(
    response: list[Any],
) -> tuple[str | None, list[tuple[str, dict[str, Any]]]]:
    return decode_event_stream_response(response, expected_kind=_LLM_OUTPUT_KIND)


class RedisLlmOutputReader(RedisEventReader):
    """Compatibility reader bound to the LLM output channel."""

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
        return cls(
            client,
            llm_output_stream_keys(config.key_prefix, workflow_id),
            expected_kind=_LLM_OUTPUT_KIND,
        )
