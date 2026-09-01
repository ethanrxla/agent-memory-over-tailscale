"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Config, load_config
from .context import AppContext
from .db import create_connection, migrate
from .routes import MODULES

log = logging.getLogger("agent_memory")

__version__ = "0.2.0"


def create_app(
    db_path: str | None = None,
    shared_key: str | None = None,
    config: Config | None = None,
    **overrides: object,
) -> FastAPI:
    """Build the hub application.

    ``db_path`` and ``shared_key`` are kept as positional-friendly keywords for
    backwards compatibility with the original ``main.create_app``.
    """
    cfg = config or load_config(db_path=db_path, shared_key=shared_key, **overrides)
    ctx = AppContext(cfg)

    # Schema work happens exactly once, here -- not on every request.
    migrate(cfg.db_path)
    conn = create_connection(cfg.db_path)
    try:
        ctx.ensure_vectors(conn)
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if cfg.worker_enabled:
            ctx.worker.start()
        try:
            yield
        finally:
            ctx.worker.stop()

    app = FastAPI(
        title="Agent Memory Service",
        version=__version__,
        description="Session-aware shared RAG memory for coding agents over Tailscale.",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.ctx = ctx
    # Retained for backwards compatibility with code that read app.state directly.
    app.state.db_path = cfg.db_path
    app.state.shared_key = cfg.shared_key

    for module in MODULES:
        module.register(app, ctx)

    return app
