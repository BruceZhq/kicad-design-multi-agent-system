"""At-least-once relay from a Redis Streams audit outbox to Kafka."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from service.kafka_audit import (
    AUDIT_SCHEMA_VERSION,
    OUTBOX_PAYLOAD_FIELD,
    AuditEventDecodeError,
    KafkaAuditEvent,
    decode_outbox_event,
)

logger = logging.getLogger(__name__)

RedisEntry = tuple[Any, Mapping[Any, Any]]


class RedisStreamsClient(Protocol):
    async def xgroup_create(self, **kwargs: Any) -> Any: ...

    async def xreadgroup(self, **kwargs: Any) -> Any: ...

    async def xautoclaim(self, **kwargs: Any) -> Any: ...

    async def xack(self, *args: Any) -> Any: ...

    async def eval(self, *args: Any) -> Any: ...


class KafkaProducer(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_and_wait(self, topic: str, **kwargs: Any) -> Any: ...


ProducerFactory = Callable[..., KafkaProducer]


class KafkaAuditRelay:
    """Relay a bounded Redis consumer-group batch to Kafka.

    A Redis entry is acknowledged only after ``send_and_wait`` succeeds. A crash
    between Kafka acknowledgement and ``XACK`` can therefore publish a duplicate.
    The stable ``audit_event_id`` Kafka key lets consumers deduplicate those records;
    this class intentionally claims at-least-once rather than exactly-once delivery.
    """

    def __init__(
        self,
        *,
        redis: RedisStreamsClient,
        bootstrap_servers: str | Sequence[str],
        topic: str,
        stream: str = "ratsnest:{registry}:audit-outbox",
        group: str = "audit-kafka-relay",
        consumer: str,
        batch_size: int = 100,
        block_ms: int = 1_000,
        claim_idle_ms: int = 60_000,
        claim_interval_seconds: float = 30.0,
        payload_field: str = OUTBOX_PAYLOAD_FIELD,
        producer_options: Mapping[str, Any] | None = None,
        producer_factory: ProducerFactory | None = None,
    ) -> None:
        if not topic or not stream or not group or not consumer:
            raise ValueError("topic, stream, group, and consumer must be non-empty")
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        if not 100 <= block_ms <= 5_000:
            raise ValueError("block_ms must be between 100 and 5000")
        if claim_idle_ms < 1_000:
            raise ValueError("claim_idle_ms must be at least 1000")
        if claim_interval_seconds <= 0:
            raise ValueError("claim_interval_seconds must be positive")

        options = dict(producer_options or {})
        if options.pop("acks", "all") != "all":
            raise ValueError("Kafka audit producer requires acks='all'")
        if options.pop("enable_idempotence", True) is not True:
            raise ValueError("Kafka audit producer requires enable_idempotence=True")

        self.redis = redis
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.claim_idle_ms = claim_idle_ms
        self.claim_interval_seconds = claim_interval_seconds
        self.payload_field = payload_field
        self.producer_options = options
        self._producer_factory = producer_factory or _default_producer_factory

        self._producer: KafkaProducer | None = None
        self._shutdown = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._claim_cursor = "0-0"
        self._next_claim_at = 0.0

    async def start(self) -> None:
        """Create the consumer group and start the idempotent Kafka producer."""
        async with self._lifecycle_lock:
            if self._producer is not None:
                return
            await self._ensure_consumer_group()
            producer = self._producer_factory(
                bootstrap_servers=self.bootstrap_servers,
                acks="all",
                enable_idempotence=True,
                **self.producer_options,
            )
            await producer.start()
            self._producer = producer

    async def run(self) -> None:
        """Run until shutdown is requested or this task is cancelled."""
        await self.start()
        retry_delay = 0.25
        try:
            while not self._shutdown.is_set():
                try:
                    processed = await self.run_once()
                    retry_delay = 0.25
                    if processed == 0:
                        await self._wait_for_shutdown(0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Kafka audit relay iteration failed")
                    await self._wait_for_shutdown(retry_delay)
                    retry_delay = min(retry_delay * 2, 10.0)
        finally:
            await asyncio.shield(self.stop())

    async def run_once(self) -> int:
        """Reclaim stale pending work, then process one bounded new-message batch."""
        if self._producer is None:
            raise RuntimeError("KafkaAuditRelay.start() must be called before run_once()")

        processed = 0
        loop = asyncio.get_running_loop()
        if loop.time() >= self._next_claim_at:
            processed += await self._reclaim_pending()
            self._next_claim_at = loop.time() + self.claim_interval_seconds

        remaining = self.batch_size - processed
        if remaining <= 0 or self._shutdown.is_set():
            return processed

        response = await self.redis.xreadgroup(
            groupname=self.group,
            consumername=self.consumer,
            streams={self.stream: ">"},
            count=remaining,
            block=self.block_ms,
        )
        entries = _readgroup_entries(response, self.stream)
        return processed + await self._publish_entries(entries[:remaining])

    def request_shutdown(self) -> None:
        """Ask the relay loop to stop after the in-flight entry is resolved."""
        self._shutdown.set()

    async def stop(self) -> None:
        """Stop accepting work and flush/close the Kafka producer."""
        self._shutdown.set()
        async with self._lifecycle_lock:
            producer, self._producer = self._producer, None
            if producer is not None:
                await producer.stop()

    async def __aenter__(self) -> KafkaAuditRelay:
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    async def _ensure_consumer_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                name=self.stream,
                groupname=self.group,
                id="0-0",
                mkstream=True,
            )
        except Exception as exc:
            # redis-py exposes a ResponseError, but keeping the boundary structural
            # avoids making the relay import Redis at module import time.
            if "BUSYGROUP" not in str(exc).upper():
                raise

    async def _reclaim_pending(self) -> int:
        response = await self.redis.xautoclaim(
            name=self.stream,
            groupname=self.group,
            consumername=self.consumer,
            min_idle_time=self.claim_idle_ms,
            start_id=self._claim_cursor,
            count=self.batch_size,
        )
        cursor, entries = _autoclaim_entries(response)
        self._claim_cursor = cursor
        return await self._publish_entries(entries[: self.batch_size])

    async def _publish_entries(self, entries: Sequence[RedisEntry]) -> int:
        published = 0
        for raw_id, fields in entries:
            if self._shutdown.is_set():
                break
            entry_id = _as_text(raw_id)
            try:
                event = decode_outbox_event(
                    fields,
                    entry_id=entry_id,
                    payload_field=self.payload_field,
                )
            except AuditEventDecodeError:
                # Never acknowledge malformed audit data: doing so would silently lose it.
                logger.exception("Invalid audit outbox entry id=%s", entry_id)
                continue

            await self._publish(event, entry_id)
            # The only acknowledgement point is after Kafka has acknowledged the send.
            acknowledged = await self.redis.eval(
                "local n=redis.call('XACK',KEYS[1],ARGV[1],ARGV[2]); "
                "if n==1 then redis.call('XDEL',KEYS[1],ARGV[2]) end; return n",
                1,
                self.stream,
                self.group,
                raw_id,
            )
            if int(acknowledged) != 1:
                raise RuntimeError("Kafka-published audit entry was not pending in Redis")
            published += 1
        return published

    async def _publish(self, event: KafkaAuditEvent, entry_id: str) -> None:
        producer = self._producer
        if producer is None:
            raise RuntimeError("Kafka producer is not running")
        await producer.send_and_wait(
            self.topic,
            key=event.kafka_key,
            value=event.to_json_bytes(),
            headers=[
                ("content-type", b"application/json"),
                ("schema-version", AUDIT_SCHEMA_VERSION.encode("ascii")),
                ("audit-event-id", event.kafka_key),
                ("redis-stream-id", entry_id.encode("utf-8")),
            ],
        )

    async def _wait_for_shutdown(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except TimeoutError:
            pass


def _default_producer_factory(**kwargs: Any) -> KafkaProducer:
    try:
        from aiokafka import AIOKafkaProducer
    except ImportError as exc:  # pragma: no cover - deployment dependency boundary
        raise RuntimeError("Kafka audit relay requires the 'aiokafka' package") from exc
    return AIOKafkaProducer(**kwargs)


def _readgroup_entries(response: Any, stream: str) -> list[RedisEntry]:
    entries: list[RedisEntry] = []
    for raw_stream, raw_entries in response or []:
        if _as_text(raw_stream) != stream:
            continue
        entries.extend(raw_entries or [])
    return entries


def _autoclaim_entries(response: Any) -> tuple[str, list[RedisEntry]]:
    if not response or len(response) < 2:
        return "0-0", []
    return _as_text(response[0]), list(response[1] or [])


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _unique_consumer_name(client_id: str) -> str:
    """Return a process-instance identity suitable for Redis consumer ownership."""
    host = "".join(
        character if character.isalnum() or character in "-." else "-"
        for character in socket.gethostname()
    ).strip("-")
    host = host or "unknown-host"
    return f"{client_id}-{host}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _kafka_security_options() -> dict[str, Any]:
    """Build aiokafka security options without rendering secrets in logs."""
    from core import settings

    protocol = settings.KAFKA_SECURITY_PROTOCOL.upper()
    allowed_protocols = {"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"}
    if protocol not in allowed_protocols:
        raise ValueError(f"Unsupported Kafka security protocol: {protocol}")

    options: dict[str, Any] = {
        "client_id": settings.KAFKA_CLIENT_ID,
        "security_protocol": protocol,
    }
    if protocol.startswith("SASL_"):
        mechanism = settings.KAFKA_SASL_MECHANISM
        username = settings.KAFKA_SASL_USERNAME
        password = settings.KAFKA_SASL_PASSWORD
        if not mechanism or not username or password is None:
            raise ValueError("SASL Kafka security requires mechanism, username, and password")
        options.update(
            {
                "sasl_mechanism": mechanism,
                "sasl_plain_username": username,
                "sasl_plain_password": password.get_secret_value(),
            }
        )
    elif any(
        (
            settings.KAFKA_SASL_MECHANISM,
            settings.KAFKA_SASL_USERNAME,
            settings.KAFKA_SASL_PASSWORD,
        )
    ):
        raise ValueError("SASL credentials require a SASL Kafka security protocol")
    return options


async def main() -> int:
    """Run the relay worker from ``python -m service.kafka_relay``."""
    from core import settings

    logging.basicConfig(
        level=settings.LOG_LEVEL.to_logging_level(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not settings.KAFKA_AUDIT_ENABLED:
        logger.error("Kafka audit relay is disabled by configuration")
        return 2
    if settings.REDIS_URL is None:
        logger.error("Kafka audit relay requires REDIS_URL")
        return 2

    try:
        from redis.asyncio import Redis
    except ImportError:
        logger.error("Kafka audit relay requires the 'redis' package")
        return 2

    bootstrap_servers = [
        server.strip() for server in settings.KAFKA_BOOTSTRAP_SERVERS.split(",") if server.strip()
    ]
    if not bootstrap_servers:
        logger.error("Kafka audit relay requires KAFKA_BOOTSTRAP_SERVERS")
        return 2
    try:
        kafka_options = _kafka_security_options()
    except ValueError as exc:
        logger.error("Invalid Kafka security configuration: %s", exc)
        return 2

    try:
        redis_client = Redis.from_url(
            settings.REDIS_URL.get_secret_value(),
            decode_responses=False,
            health_check_interval=30,
            socket_keepalive=True,
        )
    except Exception as exc:
        logger.error("Invalid Redis configuration: %s", type(exc).__name__)
        return 2
    consumer_name = _unique_consumer_name(settings.KAFKA_CLIENT_ID)
    kafka_options["client_id"] = f"{consumer_name}-producer"

    relay = KafkaAuditRelay(
        redis=redis_client,
        bootstrap_servers=bootstrap_servers,
        topic=settings.KAFKA_AUDIT_TOPIC,
        stream=f"{settings.REDIS_KEY_PREFIX.rstrip(':')}:{{registry}}:audit-outbox",
        group=settings.KAFKA_AUDIT_CONSUMER_GROUP,
        consumer=consumer_name,
        batch_size=settings.KAFKA_AUDIT_BATCH_SIZE,
        block_ms=settings.KAFKA_AUDIT_POLL_MS,
        producer_options=kafka_options,
    )

    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, relay.request_shutdown)
            installed_signals.append(signum)
        except (NotImplementedError, RuntimeError):
            # On platforms without loop signal handlers, asyncio.run still cancels
            # the main task on KeyboardInterrupt and the finally blocks close clients.
            continue

    logger.info(
        "Starting Kafka audit relay consumer=%s stream=%s topic=%s",
        consumer_name,
        relay.stream,
        relay.topic,
    )
    try:
        await relay.run()
        return 0
    except asyncio.CancelledError:
        relay.request_shutdown()
        raise
    except Exception as exc:
        # Report only the exception class. Connection exceptions can contain URLs,
        # and URLs or producer configuration may carry Redis/SASL credentials.
        logger.error("Kafka audit relay stopped after %s", type(exc).__name__)
        return 1
    finally:
        relay.request_shutdown()
        await asyncio.shield(relay.stop())
        await asyncio.shield(redis_client.aclose())
        for signum in installed_signals:
            loop.remove_signal_handler(signum)


def cli() -> None:
    try:
        exit_code = asyncio.run(main())
    except KeyboardInterrupt:
        exit_code = 130
    raise SystemExit(exit_code)


if __name__ == "__main__":
    cli()
