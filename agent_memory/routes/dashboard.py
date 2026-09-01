"""Dashboard HTML and JSON."""

from __future__ import annotations

import json
from html import escape
from importlib import resources
from string import Template
from typing import Any

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..context import AppContext
from ..models import SearchRequest
from ..retrieval import search as run_search
from ..store import format_ts

COOKIE_NAME = "agent_memory_key"


def _template() -> Template:
    return Template((resources.files("agent_memory") / "templates" / "dashboard.html").read_text("utf-8"))


def collect(ctx: AppContext, query: str | None, project_key: str | None) -> dict[str, Any]:
    conn = ctx.open()
    try:
        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()["c"]

        stats = {
            "agents": count("SELECT COUNT(*) c FROM agents"),
            "entries": count("SELECT COUNT(*) c FROM entries"),
            "projects": count("SELECT COUNT(*) c FROM projects"),
            "sessions": count("SELECT COUNT(*) c FROM sessions"),
            "events": count("SELECT COUNT(*) c FROM session_events"),
            "summaries": count("SELECT COUNT(*) c FROM session_summaries"),
            "chunks": count("SELECT COUNT(*) c FROM memory_chunks"),
            "embedded": count("SELECT COUNT(*) c FROM memory_chunks WHERE embedded_at IS NOT NULL"),
            "queued": count("SELECT COUNT(*) c FROM embedding_queue"),
            "last_updated": conn.execute(
                "SELECT MAX(ts) v FROM memory_chunks"
            ).fetchone()["v"],
        }

        sessions = conn.execute(
            """
            SELECT s.*, (
                SELECT summary FROM session_summaries WHERE session_id = s.session_id
                ORDER BY CASE tier WHEN 'final' THEN 0 ELSE 1 END LIMIT 1
            ) AS summary
            FROM sessions s WHERE s.is_sidechain = 0
            ORDER BY s.last_event_at DESC LIMIT 10
            """
        ).fetchall()

        projects = conn.execute(
            """
            SELECT p.project_key, p.display_name,
                   (SELECT COUNT(*) FROM sessions WHERE project_key = p.project_key) AS session_count,
                   (SELECT MAX(last_event_at) FROM sessions WHERE project_key = p.project_key) AS last_active
            FROM projects p ORDER BY last_active DESC NULLS LAST LIMIT 10
            """
        ).fetchall()

        agents = conn.execute(
            "SELECT display_name, device_name, tailscale_name, last_seen_at FROM agents ORDER BY last_seen_at DESC LIMIT 8"
        ).fetchall()

        entries = conn.execute(
            """
            SELECT e.entry_id, e.agent_id, e.namespace, e.kind, e.title, e.visibility, e.updated_at,
                   COALESCE((SELECT project_key FROM memory_chunks
                             WHERE source_type = 'entry' AND source_ref = e.entry_id LIMIT 1),
                            '__global__') AS project_key
            FROM entries e ORDER BY e.updated_at DESC LIMIT 12
            """
        ).fetchall()

        results: list[dict[str, Any]] = []
        search_meta: dict[str, Any] = {}
        if query:
            request = SearchRequest(
                query=query,
                project_key=project_key or None,
                scope="project" if project_key else "global",
                limit=10,
            )
            payload = run_search(conn, ctx.config, request, query_vector=ctx.query_vector(query))
            results = payload["results"]
            search_meta = {"scope": payload["scope"], "retrieval": payload["retrieval"], "message": payload["message"]}
    finally:
        conn.close()

    return {
        "stats": stats,
        "sessions": [dict(row) for row in sessions],
        "entries": [dict(row) for row in entries],
        "projects": [dict(row) for row in projects],
        "agents": [dict(row) for row in agents],
        "search": {"query": query or "", "project_key": project_key or "", "results": results, **search_meta},
        "service": ctx.status(),
    }


def render(payload: dict[str, Any]) -> str:
    stats = payload["stats"]

    sessions_html = "".join(
        "<tr>"
        f"<td><code>{escape(str(s['project_key']))}</code></td>"
        f"<td>{escape(s['ai_title'] or s['session_id'][:8])}"
        + (f"<div class='summary'>{escape((s['summary'] or '')[:180])}</div>" if s["summary"] else "")
        + "</td>"
        f"<td>{escape(str(s['tool']))}</td>"
        f"<td><span class='pill {'ok' if s['status'] == 'active' else 'muted'}'>{escape(str(s['status']))}</span></td>"
        f"<td>{s['event_count']}</td>"
        f"<td>{escape(format_ts(s['last_event_at']))}</td>"
        "</tr>"
        for s in payload["sessions"]
    ) or "<tr><td colspan='6' class='empty'>No sessions ingested yet. Start the watcher to populate this.</td></tr>"

    entries_html = "".join(
        "<tr>"
        f"<td><code>{escape(str(e['project_key']))}</code></td>"
        f"<td>{escape(str(e['kind']))}</td>"
        f"<td>{escape(str(e['title']))}</td>"
        f"<td><code>{escape(str(e['agent_id']))}</code></td>"
        f"<td>{escape(format_ts(e['updated_at']))}</td>"
        "</tr>"
        for e in payload["entries"]
    ) or "<tr><td colspan='5' class='empty'>No entries stored yet.</td></tr>"

    projects_html = "".join(
        "<tr>"
        f"<td><code>{escape(str(p['project_key']))}</code></td>"
        f"<td>{p['session_count']}</td>"
        f"<td>{escape(format_ts(p['last_active']))}</td>"
        "</tr>"
        for p in payload["projects"]
    ) or "<tr><td colspan='3' class='empty'>No projects yet.</td></tr>"

    agents_html = "".join(
        "<tr>"
        f"<td>{escape(str(a['display_name']))}</td>"
        f"<td>{escape(str(a['device_name'] or a['tailscale_name'] or '-'))}</td>"
        f"<td>{escape(format_ts(a['last_seen_at']))}</td>"
        "</tr>"
        for a in payload["agents"]
    ) or "<tr><td colspan='3' class='empty'>No agents registered.</td></tr>"

    service = payload["service"]
    coverage = (stats["embedded"] / stats["chunks"] * 100) if stats["chunks"] else 0.0
    worker = service.get("worker", {})
    health_rows = [
        ("Indexed chunks", str(stats["chunks"])),
        ("Embedding coverage", f"{coverage:.0f}% ({stats['embedded']}/{stats['chunks']})"),
        ("Embedding queue", str(stats["queued"])),
        ("Vector search", "<span class='pill ok'>on</span>" if service["sqlite_vec"] else "<span class='pill warn'>sqlite-vec missing</span>"),
        ("NVIDIA NIM", "<span class='pill ok'>on</span>" if service["nim_enabled"] else "<span class='pill warn'>no API key &mdash; keyword + extractive only</span>"),
        ("Embed model", escape(str(service["embed_model"] or "-"))),
        ("Summary model", escape(str(service["summary_model"] or "-"))),
        ("Worker", "<span class='pill ok'>running</span>" if worker.get("running") else "<span class='pill muted'>stopped</span>"),
        ("Worker cycles", str(worker.get("runs", 0))),
        ("Last worker error", escape(str(worker.get("last_error") or "none"))),
    ]
    health_html = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in health_rows)

    if payload["search"]["query"]:
        results_html = "".join(
            "<article class='search-result'>"
            f"<h3>{escape(str(r['title']))}</h3>"
            f"<p class='meta'><code>{escape(str(r['project_key']))}</code> &middot; {escape(str(r['source_type']))}"
            f" &middot; relevance {r['relevance']:.2f}"
            + (f" &middot; cosine {r['similarity']:.2f}" if r.get("similarity") is not None else "")
            + f" &middot; {escape(format_ts(r['updated_at']))}</p>"
            f"<pre>{escape(str(r['content'])[:1200])}</pre>"
            "</article>"
            for r in payload["search"]["results"]
        ) or f"<p class='empty'>{escape(str(payload['search'].get('message') or 'No search results.'))}</p>"
    else:
        results_html = "<p class='empty'>Enter a query to search shared memory.</p>"

    return _template().substitute(
        stat_projects=stats["projects"], stat_sessions=stats["sessions"], stat_events=stats["events"],
        stat_summaries=stats["summaries"], stat_entries=stats["entries"], stat_agents=stats["agents"],
        sessions_html=sessions_html, entries_html=entries_html,
        projects_html=projects_html, agents_html=agents_html,
        health_html=health_html, search_results_html=results_html,
        query_value=escape(payload["search"]["query"]),
        project_value=escape(payload["search"]["project_key"]),
        last_updated=escape(format_ts(stats["last_updated"])),
    )


def register(app: FastAPI, ctx: AppContext) -> None:
    browser_auth = ctx.require_browser_key_dep()

    @app.get("/login")
    def login(key: str = Query(...)) -> Response:
        """Exchange a key for a cookie, then redirect.

        Browsers cannot attach a custom header to a navigation, so this is the
        one place a key may appear in a URL -- and it is immediately traded for
        a cookie and redirected away so it never becomes the referrer of a
        subsequent request.
        """
        ctx.check_key(key)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            COOKIE_NAME, key, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30, path="/"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        q: str | None = Query(default=None),
        project_key: str | None = Query(default=None),
        _: None = Depends(browser_auth),
    ) -> HTMLResponse:
        return HTMLResponse(render(collect(ctx, q, project_key)))

    @app.get("/dashboard/data")
    def dashboard_json(
        q: str | None = Query(default=None),
        project_key: str | None = Query(default=None),
        _: None = Depends(browser_auth),
    ) -> dict[str, Any]:
        return collect(ctx, q, project_key)
