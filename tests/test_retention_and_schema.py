"""Retention sweep and schema presence."""

from __future__ import annotations

import sqlite3

from agent_memory.config import load_config
from agent_memory.nim import NimClient
from agent_memory.worker import run_once, sweep_retention


def _tables(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    return names


def test_all_core_tables_exist(client) -> None:
    tables = _tables(client.app.state.config.db_path)
    assert {
        "agents", "entries", "entry_chunks", "projects", "project_links",
        "sessions", "session_events", "session_summaries",
        "memory_chunks", "memory_chunks_fts", "embedding_queue", "schema_migrations",
    } <= tables


def test_retention_keeps_summaries_drops_old_events(client) -> None:
    session = {"session_id": "old", "project_key": "path:/p", "tool": "claude-code"}
    client.post("/v1/sessions/events", json={
        "session": session,
        "events": [{"seq": i, "role": "user", "kind": "prompt",
                    "content": f"event {i}", "ts": "2020-01-01T00:00:00Z"} for i in range(5)],
    })

    ctx = client.app.state.ctx
    conn = ctx.open()
    try:
        cfg = load_config(db_path=ctx.config.db_path, retention_days=30)
        purged = sweep_retention(conn, cfg)
        conn.commit()
    finally:
        conn.close()
    assert purged == 5

    detail = client.get("/v1/sessions/old").json()
    assert detail["session"]["event_count"] == 0  # events aged out
    assert detail["session"]["session_id"] == "old"  # session row survives


def test_retention_disabled_by_default(client) -> None:
    ctx = client.app.state.ctx
    conn = ctx.open()
    try:
        assert sweep_retention(conn, ctx.config) == 0  # retention_days == 0
    finally:
        conn.close()


def test_run_once_is_safe_without_nim(client) -> None:
    cfg = client.app.state.config
    result = run_once(cfg, NimClient(cfg))
    assert "summaries" in result and "embeddings" in result
    assert result["embeddings"]["embedded"] == 0  # no NIM key
