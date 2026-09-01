"""Entry point for the agent memory hub.

The implementation lives in the ``agent_memory`` package; this module stays as
the documented ``uvicorn main:app`` target and re-exports ``create_app``.

``app`` is built lazily via PEP 562 so that merely importing this module does
not touch the filesystem -- the previous module-level ``app = create_app()``
tried to create the default ``/data`` directory at import time, which made the
test suite fail to collect outside Docker.
"""

from __future__ import annotations

from agent_memory.app import create_app

__all__ = ["create_app", "app"]


def __getattr__(name: str):
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
