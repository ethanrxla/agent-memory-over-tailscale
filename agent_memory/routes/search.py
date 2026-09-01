"""Hybrid search endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from ..context import AppContext
from ..models import SearchRequest
from ..retrieval import search as run_search


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.post("/v1/search")
    def search(payload: SearchRequest, _: None = Depends(ctx.require_key_dep())) -> dict[str, Any]:
        # Embedding the query is best-effort: without it the same code path
        # runs keyword-only.
        vector = ctx.query_vector(payload.query)
        conn = ctx.open()
        try:
            return run_search(conn, ctx.config, payload, query_vector=vector)
        finally:
            conn.close()
