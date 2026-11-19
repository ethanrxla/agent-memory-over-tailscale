"""Export endpoints: let one hub's contents be replayed into another.

The owner's always-on hub lives on one OS of a dual-boot PC; when the other OS
is booted, clients fall back to a second hub with its own database. Once the
first hub is back, ``memory.py sync --from <fallback> --to <primary>`` reads
these endpoints and re-posts through the normal ingest routes, which are
idempotent: events are keyed on (session_id, seq) and entries on
(agent_id, namespace, source_id), so replaying twice changes nothing.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from ..context import AppContext


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/v1/export/sessions")
    def export_sessions(
        since: str | None = Query(default=None, description="ISO timestamp; sessions active since"),
        project_key: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        clauses, params = [], []
        if since:
            clauses.append("s.last_event_at >= ?")
            params.append(since)
        if project_key:
            clauses.append("s.project_key = ?")
            params.append(project_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = ctx.open()
        try:
            rows = conn.execute(
                f"""
                SELECT s.*, p.display_name, p.repo_root, p.git_remote
                FROM sessions s LEFT JOIN projects p ON p.project_key = s.project_key
                {where}
                ORDER BY s.last_event_at ASC, s.session_id ASC LIMIT ? OFFSET ?
                """,
                [*params, limit + 1, offset],
            ).fetchall()
        finally:
            conn.close()
        more = len(rows) > limit
        sessions = [
            {
                "session_id": r["session_id"], "project_key": r["project_key"],
                "display_name": r["display_name"], "repo_root": r["repo_root"],
                "git_remote": r["git_remote"], "agent_id": r["agent_id"],
                "device_name": r["device_name"], "tool": r["tool"], "cwd": r["cwd"],
                "git_branch": r["git_branch"], "ai_title": r["ai_title"],
                "last_prompt": (r["last_prompt"] or None) and r["last_prompt"][:4000],
                "status": r["status"], "is_sidechain": bool(r["is_sidechain"]),
                "started_at": r["started_at"], "metadata": _json(r["metadata_json"], {}),
                "last_event_at": r["last_event_at"], "event_count": r["event_count"],
            }
            for r in rows[:limit]
        ]
        return {"sessions": sessions, "next_offset": offset + limit if more else None}

    @app.get("/v1/export/sessions/{session_id:path}/events")
    def export_events(
        session_id: str,
        after_seq: int = Query(default=-1),
        limit: int = Query(default=1000, ge=1, le=2000),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            if conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="Session not found")
            rows = conn.execute(
                """
                SELECT seq, ts, role, kind, content, files_json, commands_json, is_sidechain
                FROM session_events WHERE session_id = ? AND seq > ?
                ORDER BY seq ASC LIMIT ?
                """,
                (session_id, after_seq, limit),
            ).fetchall()
        finally:
            conn.close()
        return {
            "session_id": session_id,
            "events": [
                {
                    "seq": r["seq"], "ts": r["ts"], "role": r["role"], "kind": r["kind"],
                    "content": r["content"], "files": _json(r["files_json"], []),
                    "commands": _json(r["commands_json"], []), "is_sidechain": bool(r["is_sidechain"]),
                }
                for r in rows
            ],
        }

    @app.get("/v1/export/entries")
    def export_entries(
        since: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        where = "WHERE e.updated_at >= ?" if since else ""
        params: list[Any] = [since] if since else []
        conn = ctx.open()
        try:
            rows = conn.execute(
                f"""
                SELECT e.*, a.display_name AS agent_display_name, a.device_name AS agent_device,
                       a.tailscale_name AS agent_tailscale, a.capabilities_json AS agent_caps,
                       COALESCE((SELECT project_key FROM memory_chunks
                                 WHERE source_type = 'entry' AND source_ref = e.entry_id LIMIT 1),
                                '__global__') AS project_key
                FROM entries e LEFT JOIN agents a ON a.agent_id = e.agent_id
                {where}
                ORDER BY e.updated_at ASC, e.entry_id ASC LIMIT ? OFFSET ?
                """,
                [*params, limit + 1, offset],
            ).fetchall()
        finally:
            conn.close()
        more = len(rows) > limit
        entries = [
            {
                "agent_id": r["agent_id"], "namespace": r["namespace"], "source_id": r["source_id"],
                "kind": r["kind"], "title": r["title"], "content": r["content"],
                "tags": _json(r["tags_json"], []), "recipient_id": r["recipient_id"],
                "visibility": r["visibility"], "source_uri": r["source_uri"],
                "metadata": _json(r["metadata_json"], {}), "project_key": r["project_key"],
                "updated_at": r["updated_at"],
                "agent": {
                    "agent_id": r["agent_id"],
                    "display_name": r["agent_display_name"] or r["agent_id"],
                    "device_name": r["agent_device"],
                    "tailscale_name": r["agent_tailscale"],
                    "capabilities": _json(r["agent_caps"], []),
                },
            }
            for r in rows[:limit]
        ]
        return {"entries": entries, "next_offset": offset + limit if more else None}
