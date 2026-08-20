"""Tenant-scoped, provenance-first cross-conversation semantic memory."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core.settings import settings
from memory.postgres import get_postgres_connection_string

_SCOPE_RE = re.compile(r"^[0-9a-f]{16}$")
_WHITESPACE_RE = re.compile(r"\s+")
_LABELLED_FACT_RE = re.compile(
    r"(?im)^\s*(project_name|run_name|language|语言|输出语言|单位制|偏好)\s*[:：=]\s*(.{1,500})$"
)
_FACT_KEYS = {
    "project_name": "project_name",
    "run_name": "run_name",
    "language": "language",
    "语言": "language",
    "输出语言": "language",
    "单位制": "unit_system",
    "偏好": "preference",
}


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory_id: str
    memory_type: str
    memory_key: str
    summary: str
    source_type: str
    occurred_at: datetime
    score: float
    same_project: bool


class MemoryEmbedder:
    """OpenAI-compatible embeddings with a deterministic, dependency-free fallback."""

    def __init__(self, dimensions: int) -> None:
        if dimensions != 384:
            raise ValueError("conversation memory schema currently requires 384 dimensions")
        self.dimensions = dimensions

    async def embed(self, text: str) -> list[float]:
        base_url = (settings.LONG_TERM_MEMORY_EMBEDDING_BASE_URL or "").rstrip("/")
        model = (settings.LONG_TERM_MEMORY_EMBEDDING_MODEL or "").strip()
        if base_url and model:
            endpoint = f"{base_url}/embeddings"
            key = settings.LONG_TERM_MEMORY_EMBEDDING_API_KEY
            headers = {"Authorization": f"Bearer {key.get_secret_value()}"} if key else {}
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        endpoint,
                        headers=headers,
                        json={"model": model, "input": text[:8_000]},
                    )
                    response.raise_for_status()
                    payload = response.json()
                vector = payload["data"][0]["embedding"]
                if len(vector) != self.dimensions:
                    raise ValueError("embedding endpoint returned an unexpected dimension")
                return _normalize_vector([float(value) for value in vector])
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                # Memory is advisory. Provider failure must not block an engineering run.
                pass
        return _hash_embedding(text, self.dimensions)


class LongTermMemory:
    """Persistent hybrid retrieval with provenance, decay, and conflict handling."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self._embedder = MemoryEmbedder(settings.LONG_TERM_MEMORY_EMBEDDING_DIMENSIONS)

    async def healthcheck(self) -> None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT to_regclass('control_plane.conversation_memories') IS NOT NULL AS ready"
            )
            row = await cursor.fetchone()
            if not row or not row["ready"]:
                raise RuntimeError("conversation memory schema is unavailable; run Flyway V17")

    async def record_user_event(
        self,
        *,
        tenant_scope: str,
        principal_scope: str,
        project_scope: str,
        thread_id: str,
        request_id: str,
        message: str,
        occurred_at: datetime | None = None,
    ) -> None:
        _validate_scopes(tenant_scope, principal_scope, project_scope)
        normalized = _normalize_summary(message)
        if not normalized:
            return
        timestamp = occurred_at or datetime.now(UTC)
        records: list[tuple[str, str, str, dict[str, Any], float]] = [
            (
                "episodic",
                f"event:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:24]}",
                normalized,
                {},
                1.0,
            )
        ]
        for match in _LABELLED_FACT_RE.finditer(message):
            key = _FACT_KEYS[match.group(1).casefold()]
            value = _normalize_summary(match.group(2), limit=500)
            if value:
                records.append(("user_fact", key, f"{key}: {value}", {"value": value}, 1.0))

        prepared = [
            (
                memory_type,
                memory_key,
                summary,
                value_json,
                confidence,
                await self._embedder.embed(summary),
            )
            for memory_type, memory_key, summary, value_json, confidence in records
        ]
        async with self._pool.connection() as connection:
            async with connection.transaction():
                for (
                    memory_type,
                    memory_key,
                    summary,
                    value_json,
                    confidence,
                    embedding,
                ) in prepared:
                    source_hash = hashlib.sha256(
                        f"user_statement\0{request_id}\0{memory_key}\0{summary}".encode()
                    ).hexdigest()
                    superseded: list[str] = []
                    if memory_type == "user_fact":
                        cursor = await connection.execute(
                            """
                            SELECT memory_id::text, value_json
                            FROM control_plane.conversation_memories
                            WHERE tenant_scope = %s AND principal_scope = %s
                              AND memory_key = %s AND status = 'active'
                            FOR UPDATE
                            """,
                            (tenant_scope, principal_scope, memory_key),
                        )
                        for existing in await cursor.fetchall():
                            if existing["value_json"] != value_json:
                                superseded.append(existing["memory_id"])
                        if superseded:
                            await connection.execute(
                                """
                                UPDATE control_plane.conversation_memories
                                SET status = 'superseded', updated_at = clock_timestamp()
                                WHERE memory_id = ANY(%s::uuid[])
                                """,
                                (superseded,),
                            )
                            value_json = {**value_json, "supersedes": superseded}
                    await connection.execute(
                        """
                        INSERT INTO control_plane.conversation_memories (
                            memory_id, tenant_scope, principal_scope, project_scope,
                            thread_id, request_id, memory_type, memory_key, summary,
                            value_json, embedding, source_type, source_sha256,
                            confidence, occurred_at, expires_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::jsonb, %s::vector, 'user_statement', %s, %s, %s,
                            %s + make_interval(days => %s)
                        )
                        ON CONFLICT (tenant_scope, principal_scope, source_sha256) DO NOTHING
                        """,
                        (
                            str(uuid4()), tenant_scope, principal_scope, project_scope,
                            thread_id[:200], request_id[:200], memory_type, memory_key,
                            summary, json.dumps(value_json, ensure_ascii=False),
                            _vector_literal(embedding), source_hash, confidence, timestamp,
                            timestamp, settings.LONG_TERM_MEMORY_RETENTION_DAYS,
                        ),
                    )

    async def search(
        self,
        *,
        tenant_scope: str,
        principal_scope: str,
        project_scope: str,
        query: str,
    ) -> list[RetrievedMemory]:
        _validate_scopes(tenant_scope, principal_scope, project_scope)
        normalized = _normalize_summary(query)
        if not normalized:
            return []
        embedding = await self._embedder.embed(normalized)
        limit = settings.LONG_TERM_MEMORY_RETRIEVAL_LIMIT
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                WITH semantic AS (
                    SELECT memory_id, memory_type, memory_key, summary, source_type,
                           occurred_at, project_scope, confidence,
                           GREATEST(0, 1 - (embedding <=> %s::vector)) AS semantic_score,
                           ts_rank_cd(search_document, plainto_tsquery('simple', %s)) AS lexical_score
                    FROM control_plane.conversation_memories
                    WHERE tenant_scope = %s AND principal_scope = %s
                      AND status = 'active'
                      AND (expires_at IS NULL OR expires_at > clock_timestamp())
                    ORDER BY embedding <=> %s::vector
                    LIMIT 64
                ), ranked AS (
                    SELECT *,
                        LEAST(1, 0.65 * semantic_score
                            + 0.20 * LEAST(1, lexical_score)
                            + 0.10 * power(0.5, GREATEST(0,
                                extract(epoch from (clock_timestamp() - occurred_at)) / 86400.0
                                / %s))
                            + 0.05 * confidence
                            + CASE WHEN project_scope = %s THEN 0.05 ELSE 0 END) AS score
                    FROM semantic
                )
                SELECT memory_id::text, memory_type, memory_key, summary, source_type,
                       occurred_at, project_scope, score
                FROM ranked
                WHERE score >= %s
                ORDER BY score DESC, occurred_at DESC, memory_id
                LIMIT %s
                """,
                (
                    _vector_literal(embedding), normalized, tenant_scope, principal_scope,
                    _vector_literal(embedding),
                    settings.LONG_TERM_MEMORY_RECENCY_HALF_LIFE_DAYS,
                    project_scope, settings.LONG_TERM_MEMORY_MIN_SCORE, limit,
                ),
            )
            rows = await cursor.fetchall()
            ids = [row["memory_id"] for row in rows]
            if ids:
                await connection.execute(
                    """
                    UPDATE control_plane.conversation_memories
                    SET last_accessed_at = clock_timestamp()
                    WHERE memory_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
        return [
            RetrievedMemory(
                memory_id=row["memory_id"],
                memory_type=row["memory_type"],
                memory_key=row["memory_key"],
                summary=row["summary"],
                source_type=row["source_type"],
                occurred_at=row["occurred_at"],
                score=float(row["score"]),
                same_project=row["project_scope"] == project_scope,
            )
            for row in rows
        ]

    async def record_verified_outcome(
        self,
        *,
        tenant_scope: str,
        principal_scope: str,
        project_scope: str,
        thread_id: str,
        request_id: str,
        delivery_status: str,
        artifact_count: int,
        blockers: list[str],
    ) -> None:
        """Persist a deterministic terminal event; never summarize model prose."""
        _validate_scopes(tenant_scope, principal_scope, project_scope)
        bounded_blockers = [_normalize_summary(item, limit=300) for item in blockers[:8] if item]
        summary = _normalize_summary(
            f"Run {request_id} delivery={delivery_status}; artifacts={artifact_count}; "
            f"blockers={'; '.join(bounded_blockers) if bounded_blockers else 'none'}"
        )
        embedding = await self._embedder.embed(summary)
        source_hash = hashlib.sha256(
            f"verified_outcome\0{request_id}\0{summary}".encode()
        ).hexdigest()
        async with self._pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO control_plane.conversation_memories (
                    memory_id, tenant_scope, principal_scope, project_scope,
                    thread_id, request_id, memory_type, memory_key, summary,
                    value_json, embedding, source_type, source_sha256,
                    confidence, occurred_at, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'outcome', %s, %s,
                    %s::jsonb, %s::vector, 'verified_outcome', %s, 1.0, %s,
                    %s + make_interval(days => %s)
                )
                ON CONFLICT (tenant_scope, principal_scope, source_sha256) DO NOTHING
                """,
                (
                    str(uuid4()), tenant_scope, principal_scope, project_scope,
                    thread_id[:200], request_id[:200], f"outcome:{request_id[:160]}",
                    summary,
                    json.dumps(
                        {
                            "delivery_status": delivery_status,
                            "artifact_count": artifact_count,
                            "blockers": bounded_blockers,
                        },
                        ensure_ascii=False,
                    ),
                    _vector_literal(embedding), source_hash, datetime.now(UTC),
                    datetime.now(UTC), settings.LONG_TERM_MEMORY_RETENTION_DAYS,
                ),
            )


def render_memory_context(items: list[RetrievedMemory]) -> str:
    """Render bounded data, never instructions, for downstream model context."""
    payload = [
        {
            "memory_id": item.memory_id,
            "type": item.memory_type,
            "key": item.memory_key,
            "summary": item.summary,
            "source": item.source_type,
            "occurred_at": item.occurred_at.isoformat(),
            "score": round(item.score, 4),
            "same_project": item.same_project,
        }
        for item in items
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@asynccontextmanager
async def initialize_long_term_memory() -> AsyncIterator[LongTermMemory | None]:
    if not settings.LONG_TERM_MEMORY_ENABLED:
        yield None
        return
    pool = AsyncConnectionPool(
        get_postgres_connection_string(),
        min_size=settings.POSTGRES_MIN_CONNECTIONS_PER_POOL,
        max_size=settings.POSTGRES_MAX_CONNECTIONS_PER_POOL,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "application_name": f"{settings.POSTGRES_APPLICATION_NAME}-memory",
        },
        check=AsyncConnectionPool.check_connection,
    )
    async with pool:
        memory = LongTermMemory(pool)
        await memory.healthcheck()
        yield memory


def _normalize_summary(value: str, *, limit: int = 1800) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()[:limit]


def _validate_scopes(*scopes: str) -> None:
    if any(not _SCOPE_RE.fullmatch(scope) for scope in scopes):
        raise ValueError("memory scopes must be authenticated opaque identifiers")


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _normalize_vector(values: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(magnitude) or magnitude <= 0:
        raise ValueError("embedding vector has no finite magnitude")
    return [value / magnitude for value in values]


def _hash_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    normalized = f"  {_normalize_summary(text).casefold()}  "
    tokens = re.findall(r"[\w.+/-]+", normalized, flags=re.UNICODE)
    features = tokens + [normalized[index : index + 3] for index in range(len(normalized) - 2)]
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    if not any(vector):
        vector[0] = 1.0
    return _normalize_vector(vector)


__all__ = [
    "LongTermMemory",
    "MemoryEmbedder",
    "RetrievedMemory",
    "initialize_long_term_memory",
    "render_memory_context",
]
