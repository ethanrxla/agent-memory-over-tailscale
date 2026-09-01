"""Shared application context passed to route modules."""

from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from fastapi import Cookie, Header, HTTPException

from .config import Config
from .db import create_connection, ensure_vector_table, sqlite_vec_available
from .nim import NimClient
from .worker import BackgroundWorker


class AppContext:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.nim = NimClient(config)
        self.worker = BackgroundWorker(config, self.nim)

    def open(self) -> sqlite3.Connection:
        """A fresh connection per request.

        Schema creation happens once at startup, not here -- the previous
        implementation re-ran the entire schema script on every single request.
        """
        return create_connection(self.config.db_path)

    def ensure_vectors(self, conn: sqlite3.Connection) -> bool:
        return ensure_vector_table(conn, self.config.embed_dim)

    # -- auth ---------------------------------------------------------------

    def check_key(self, provided: str | None) -> None:
        """Constant-time shared-key check.

        The original used ``!=``, which leaks key material through timing.
        """
        expected = self.config.shared_key
        if not expected:
            return
        if provided is None or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Invalid shared key")

    def require_key_dep(self):
        def dependency(x_shared_key: str | None = Header(default=None)) -> None:
            self.check_key(x_shared_key)

        return dependency

    def require_browser_key_dep(self):
        """Dashboard auth.

        Accepts the key from a header or a cookie, never from the query string:
        a ``?key=`` value lands in proxy access logs, browser history, and
        outbound Referer headers. Browsers cannot set a header on a plain
        navigation, so ``/login?key=...`` sets the cookie once and redirects.
        """

        def dependency(
            x_shared_key: str | None = Header(default=None),
            agent_memory_key: str | None = Cookie(default=None),
        ) -> None:
            self.check_key(x_shared_key if x_shared_key is not None else agent_memory_key)

        return dependency

    def query_vector(self, query: str) -> list[float] | None:
        """Embed a search query, or None when embeddings are unavailable."""
        if not self.nim.enabled or not sqlite_vec_available():
            return None
        vectors = self.nim.embed([query], input_type="query")
        if not vectors:
            return None
        return vectors[0]

    def status(self) -> dict[str, Any]:
        return {
            "db_path": self.config.db_path,
            "sqlite_vec": sqlite_vec_available(),
            "nim_enabled": self.nim.enabled,
            "embed_model": self.config.embed_model if self.nim.enabled else None,
            "summary_model": self.config.summary_model if self.nim.enabled else None,
            "worker": self.worker.status(),
        }
