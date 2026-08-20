from memory.postgres import get_postgres_saver


def initialize_database():
    """Return the PostgreSQL checkpointer used by the production runtime."""
    return get_postgres_saver()

__all__ = ["initialize_database"]
