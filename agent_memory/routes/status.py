"""Health and status endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from ..context import AppContext


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/health")
    def health(_: None = Depends(ctx.require_key_dep())) -> dict[str, Any]:
        return {"status": "ok", "db_path": ctx.config.db_path}

    @app.get("/v1/status")
    def status(_: None = Depends(ctx.require_key_dep())) -> dict[str, Any]:
        conn = ctx.open()
        try:
            counts = {
                "agents": conn.execute("SELECT COUNT(*) c FROM agents").fetchone()["c"],
                "entries": conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"],
                "projects": conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"],
                "sessions": conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"],
                "session_events": conn.execute("SELECT COUNT(*) c FROM session_events").fetchone()["c"],
                "summaries": conn.execute("SELECT COUNT(*) c FROM session_summaries").fetchone()["c"],
                "chunks": conn.execute("SELECT COUNT(*) c FROM memory_chunks").fetchone()["c"],
                "embedded_chunks": conn.execute(
                    "SELECT COUNT(*) c FROM memory_chunks WHERE embedded_at IS NOT NULL"
                ).fetchone()["c"],
                "embedding_queue": conn.execute("SELECT COUNT(*) c FROM embedding_queue").fetchone()["c"],
            }
        finally:
            conn.close()
        coverage = (
            counts["embedded_chunks"] / counts["chunks"] if counts["chunks"] else 0.0
        )
        return {**ctx.status(), "counts": counts, "embedding_coverage": round(coverage, 4)}
