from memory.long_term import LongTermMemory, initialize_long_term_memory, render_memory_context
from memory.postgres import get_postgres_saver


def initialize_database():
    """Return the PostgreSQL checkpointer used by the production runtime."""
    return get_postgres_saver()

__all__ = [
    "LongTermMemory",
    "initialize_database",
    "initialize_long_term_memory",
    "render_memory_context",
]
