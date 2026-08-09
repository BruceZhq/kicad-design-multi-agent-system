from memory.postgres import get_postgres_saver, get_postgres_store


def initialize_database():
    """Return the PostgreSQL checkpointer used by the production runtime."""
    return get_postgres_saver()


def initialize_store():
    """Return the PostgreSQL long-term store used by the production runtime."""
    return get_postgres_store()


__all__ = ["initialize_database", "initialize_store"]
