"""Versioned audit event contract shared by the Redis outbox and Kafka relay."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from service.runtime_identity import audit_scopes

AUDIT_SCHEMA_VERSION = "1.0"
OUTBOX_PAYLOAD_FIELD = "payload"


class AuditEventDecodeError(ValueError):
    """An outbox entry does not contain a valid, versioned audit event."""


class KafkaAuditEvent(BaseModel):
    """Security-conscious metadata envelope published to the audit Kafka topic.

    ``audit_event_id`` is assigned before the event enters the Redis outbox. It is
    deliberately preserved across relay attempts. Consumers must use it for
    deduplication because the relay is at-least-once, not exactly-once. Kafka is
    partitioned by request when possible so one run's lifecycle keeps its order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["1.0"] = AUDIT_SCHEMA_VERSION
    audit_event_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=200)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = Field(min_length=1, max_length=200)
    outcome: str | None = Field(default=None, max_length=100)
    actor_id: str | None = Field(default=None, max_length=200)
    tenant_id: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    correlation_id: str | None = Field(default=None, max_length=200)
    resource_type: str | None = Field(default=None, max_length=200)
    resource_id: str | None = Field(default=None, max_length=500)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @property
    def kafka_key(self) -> bytes:
        return (self.request_id or self.audit_event_id).encode("utf-8")

    def to_json_bytes(self) -> bytes:
        """Serialize deterministically for reproducible audit records."""
        payload = self.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def decode_outbox_event(
    fields: Mapping[Any, Any],
    *,
    entry_id: str,
    payload_field: str = OUTBOX_PAYLOAD_FIELD,
) -> KafkaAuditEvent:
    """Decode a Redis Stream entry without inventing a missing audit identity.

    The canonical outbox shape is one JSON value in the ``payload`` field. Direct
    model fields are also accepted to ease migrations from field-per-property
    producers. Redis entry IDs are intentionally not substituted for a missing
    ``audit_event_id``: identity must be stable before publication to the outbox.
    """
    normalized = {_as_text(key): _as_text(value) for key, value in fields.items()}
    try:
        if payload_field in normalized:
            return KafkaAuditEvent.model_validate_json(normalized[payload_field])

        # RedisRunRegistry originally writes a compact field-per-property envelope
        # from Lua. Normalize that envelope at this boundary so the Kafka contract
        # stays strict and versioned without duplicating JSON construction in every
        # state-transition script.
        if "event_id" in normalized:
            metadata_keys = {
                "agent_id",
                "thread_id",
                "status",
                "owner_id",
                "fencing_token",
                "error_code",
                "detail_digest",
            }
            tenant_scope, project_scope = audit_scopes(normalized.get("identity_scope"))
            metadata = {
                key: normalized[key] for key in metadata_keys if normalized.get(key)
            }
            if project_scope is not None:
                metadata["project_scope"] = project_scope
            return KafkaAuditEvent.model_validate(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "audit_event_id": normalized["event_id"],
                    "event_type": normalized.get("event_type", "RunEvent"),
                    "occurred_at": normalized.get("timestamp"),
                    "source": normalized.get("source", "service.run_registry"),
                    "outcome": normalized.get("status") or None,
                    "tenant_id": tenant_scope,
                    "request_id": normalized.get("request_id") or None,
                    "resource_type": "agent_run",
                    "resource_id": normalized.get("request_id") or None,
                    "metadata": metadata,
                }
            )

        model_fields = set(KafkaAuditEvent.model_fields)
        candidate: dict[str, Any] = {
            key: value for key, value in normalized.items() if key in model_fields
        }
        if "metadata" in candidate:
            candidate["metadata"] = json.loads(candidate["metadata"])
        return KafkaAuditEvent.model_validate(candidate)
    except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Do not include the event body in the exception; audit metadata may be sensitive.
        raise AuditEventDecodeError(f"invalid audit outbox entry {entry_id}") from exc


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    return str(value)
