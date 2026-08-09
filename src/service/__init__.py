from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    """Load the FastAPI graph only when the application export is requested."""

    if name != "app":
        raise AttributeError(name)
    from service.service import app

    return app
