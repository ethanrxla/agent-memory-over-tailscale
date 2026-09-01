"""Session ingest, session/project reads, and the project brief."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from ..context import AppContext
from ..models import ProjectLinkRequest, SessionEventsRequest, SessionUpsertRequest
from ..store import insert_events, upsert_project, upsert_session, utc_now
from ..summarize import project_brief


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.post("/v1/sessions/upsert")
    def upsert(payload: SessionUpsertRequest, _: None = Depends(ctx.require_key_dep())) -> dict[str, Any]:
        conn = ctx.open()
        try:
            upsert_session(conn, payload)
            conn.commit()
        finally:
            conn.close()
        return {"status": "ok", "session_id": payload.session_id, "project_key": payload.project_key}

    @app.post("/v1/sessions/events")
    def ingest_events(
        payload: SessionEventsRequest, _: None = Depends(ctx.require_key_dep())
    ) -> dict[str, Any]:
        """Idempotent batch ingest from a device watcher.

        Re-sending events after an offline spool replay is safe: duplicates
        collide on (session_id, seq) and are counted as skipped.
        """
        conn = ctx.open()
        try:
            upsert_session(conn, payload.session)
            stats = insert_events(conn, payload.session.session_id, payload.events)
            conn.commit()
        finally:
            conn.close()
        return {"status": "ok", "session_id": payload.session.session_id, **stats}

    @app.get("/v1/sessions")
    def list_sessions(
        project_key: str | None = Query(default=None),
        status: str | None = Query(default=None),
        tool: str | None = Query(default=None),
        include_sidechain: bool = Query(default=False),
        limit: int = Query(default=20, ge=1, le=100),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_key:
            clauses.append("s.project_key = ?")
            params.append(project_key)
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        if tool:
            clauses.append("s.tool = ?")
            params.append(tool)
        if not include_sidechain:
            clauses.append("s.is_sidechain = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = ctx.open()
        try:
            rows = conn.execute(
                f"""
                SELECT s.*, (
                    SELECT summary FROM session_summaries
                    WHERE session_id = s.session_id
                    ORDER BY CASE tier WHEN 'final' THEN 0 ELSE 1 END LIMIT 1
                ) AS summary
                FROM sessions s {where}
                ORDER BY s.last_event_at DESC LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        finally:
            conn.close()

        return {
            "sessions": [
                {
                    "session_id": row["session_id"],
                    "project_key": row["project_key"],
                    "tool": row["tool"],
                    "device_name": row["device_name"],
                    "agent_id": row["agent_id"],
                    "cwd": row["cwd"],
                    "git_branch": row["git_branch"],
                    "title": row["ai_title"],
                    "last_prompt": row["last_prompt"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "last_event_at": row["last_event_at"],
                    "event_count": row["event_count"],
                    "token_estimate": row["token_estimate"],
                    "summary": row["summary"],
                }
                for row in rows
            ]
        }

    @app.get("/v1/sessions/{session_id}")
    def get_session(
        session_id: str,
        detail: str = Query(default="summary", pattern="^(summary|full)$"),
        event_limit: int = Query(default=100, ge=1, le=1000),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Session not found")
            summaries = conn.execute(
                "SELECT * FROM session_summaries WHERE session_id = ? ORDER BY generated_at DESC",
                (session_id,),
            ).fetchall()
            events: list[dict[str, Any]] = []
            if detail == "full":
                event_rows = conn.execute(
                    """
                    SELECT seq, ts, role, kind, content, files_json, commands_json
                    FROM session_events WHERE session_id = ?
                    ORDER BY seq DESC LIMIT ?
                    """,
                    (session_id, event_limit),
                ).fetchall()
                events = [
                    {
                        "seq": e["seq"], "ts": e["ts"], "role": e["role"], "kind": e["kind"],
                        "content": e["content"], "files": _json_list(e["files_json"]),
                        "commands": _json_list(e["commands_json"]),
                    }
                    for e in reversed(event_rows)
                ]
        finally:
            conn.close()

        return {
            "session": {
                "session_id": row["session_id"],
                "project_key": row["project_key"],
                "tool": row["tool"],
                "device_name": row["device_name"],
                "cwd": row["cwd"],
                "git_branch": row["git_branch"],
                "title": row["ai_title"],
                "last_prompt": row["last_prompt"],
                "status": row["status"],
                "started_at": row["started_at"],
                "last_event_at": row["last_event_at"],
                "event_count": row["event_count"],
                "token_estimate": row["token_estimate"],
            },
            "summaries": [
                {
                    "tier": s["tier"], "summary": s["summary"], "model": s["model"],
                    "decisions": _json_list(s["decisions_json"]),
                    "open_threads": _json_list(s["open_threads_json"]),
                    "files": _json_list(s["files_json"]),
                    "generated_at": s["generated_at"],
                }
                for s in summaries
            ],
            "events": events,
        }

    @app.get("/v1/projects")
    def list_projects(
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            rows = conn.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM sessions WHERE project_key = p.project_key) AS session_count,
                       (SELECT MAX(last_event_at) FROM sessions WHERE project_key = p.project_key) AS last_active
                FROM projects p ORDER BY last_active DESC NULLS LAST LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return {
            "projects": [
                {
                    "project_key": r["project_key"], "display_name": r["display_name"],
                    "repo_root": r["repo_root"], "git_remote": r["git_remote"],
                    "session_count": r["session_count"], "last_active": r["last_active"],
                }
                for r in rows
            ]
        }

    @app.get("/v1/projects/{project_key:path}/brief")
    def brief(
        project_key: str,
        branch: str | None = Query(default=None),
        max_sessions: int = Query(default=5, ge=1, le=20),
        token_budget: int | None = Query(default=None, ge=100, le=4000),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            return project_brief(
                conn, ctx.config, project_key,
                token_budget=token_budget, max_sessions=max_sessions, branch=branch,
            )
        finally:
            conn.close()

    @app.get("/v1/projects/{project_key:path}/threads")
    def threads(project_key: str, _: None = Depends(ctx.require_key_dep())) -> dict[str, Any]:
        conn = ctx.open()
        try:
            rows = conn.execute(
                """
                SELECT s.session_id, s.ai_title, s.last_event_at, sm.open_threads_json
                FROM sessions s
                JOIN session_summaries sm ON sm.session_id = s.session_id
                WHERE s.project_key = ? AND sm.open_threads_json != '[]'
                ORDER BY s.last_event_at DESC LIMIT 20
                """,
                (project_key,),
            ).fetchall()
        finally:
            conn.close()
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            for thread in _json_list(row["open_threads_json"]):
                if thread in seen:
                    continue
                seen.add(thread)
                items.append({
                    "thread": thread,
                    "session_id": row["session_id"],
                    "session_title": row["ai_title"],
                    "last_event_at": row["last_event_at"],
                })
        return {"project_key": project_key, "open_threads": items}

    @app.post("/v1/projects/link")
    def link_projects(
        payload: ProjectLinkRequest, _: None = Depends(ctx.require_key_dep())
    ) -> dict[str, Any]:
        """Opt in to cross-project recall for scope='linked' queries."""
        now = utc_now()
        conn = ctx.open()
        try:
            upsert_project(conn, payload.project_key)
            upsert_project(conn, payload.linked_project_key)
            pairs = [(payload.project_key, payload.linked_project_key)]
            if payload.bidirectional:
                pairs.append((payload.linked_project_key, payload.project_key))
            for source, target in pairs:
                conn.execute(
                    """
                    INSERT INTO project_links (project_key, linked_project_key, note, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_key, linked_project_key) DO UPDATE SET note = excluded.note
                    """,
                    (source, target, payload.note, now),
                )
            conn.commit()
        finally:
            conn.close()
        return {"status": "linked", "pairs": len(pairs)}
