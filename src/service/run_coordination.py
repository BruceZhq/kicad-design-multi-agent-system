"""Process-local coordination for checkpointed graph runs."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote


@dataclass
class _ThreadRunSlot:
    lock: asyncio.Lock
    users: int = 0


_slots: dict[tuple[str, str, str], _ThreadRunSlot] = {}
_slots_guard = asyncio.Lock()


async def _drop_slot_user(
    key: tuple[str, str, str],
    slot: _ThreadRunSlot,
) -> None:
    async with _slots_guard:
        slot.users -= 1
        if slot.users == 0 and _slots.get(key) is slot:
            del _slots[key]


@asynccontextmanager
async def serialize_thread_run(
    agent_id: str,
    thread_id: str,
    *,
    user_id: str | None = None,
):
    """Serialize checkpoint writers locally and across PostgreSQL instances."""
    user_scope = user_id or ""
    key = (agent_id, user_scope, thread_id)
    async with _slots_guard:
        slot = _slots.get(key)
        if slot is None:
            slot = _ThreadRunSlot(lock=asyncio.Lock())
            _slots[key] = slot
        slot.users += 1
    try:
        await slot.lock.acquire()
    except BaseException:
        await _drop_slot_user(key, slot)
        raise
    try:
        async with _distributed_thread_lock(agent_id, user_scope, thread_id):
            yield
    finally:
        slot.lock.release()
        await _drop_slot_user(key, slot)


@asynccontextmanager
async def _distributed_thread_lock(agent_id: str, user_scope: str, thread_id: str):
    from psycopg import AsyncConnection

    from memory.postgres import get_postgres_connection_string

    digest = hashlib.blake2b(
        f"{agent_id}\0{user_scope}\0{thread_id}".encode(),
        digest_size=8,
    ).digest()
    lock_key = int.from_bytes(digest, byteorder="big", signed=True)
    connection = await AsyncConnection.connect(
        get_postgres_connection_string(),
        autocommit=True,
    )
    acquired = False
    try:
        await connection.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        acquired = True
        yield
    finally:
        if acquired:
            await asyncio.shield(
                connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            )
        await connection.close()


def scoped_checkpoint_thread_id(
    agent_id: str,
    user_id: str,
    client_thread_id: str,
) -> str:
    """Build an unambiguous checkpoint key for one agent, user, and client thread."""
    parts = (agent_id, user_id, client_thread_id)
    return "v2:" + ":".join(quote(part, safe="") for part in parts)


def checkpoint_thread_candidates(
    agent_id: str,
    user_id: str,
    client_thread_id: str,
    *,
    allow_legacy: bool = True,
) -> tuple[str, ...]:
    """Return the scoped key first, followed by the two legacy key formats."""
    scoped = scoped_checkpoint_thread_id(agent_id, user_id, client_thread_id)
    if not allow_legacy:
        return (scoped,)
    return (scoped, client_thread_id, f"{agent_id}:{client_thread_id}")
