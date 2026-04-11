import json
import os
import re
import sqlite3
import uuid
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def chunk_text(text: str, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    text = text.strip()
    if not text:
        return []

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= chunk_size:
            current = paragraph
            continue

        start = 0
        while start < len(paragraph):
            stop = min(len(paragraph), start + chunk_size)
            chunks.append(paragraph[start:stop].strip())
            if stop >= len(paragraph):
                break
            start = max(stop - overlap, start + 1)
        current = ""

    if current:
        chunks.append(current)

    return chunks


def normalize_tags(tags: list[str]) -> list[str]:
    cleaned = []
    for tag in tags:
        value = tag.strip().lower()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def normalize_fts_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_./:-]+", query)
    if not tokens:
        raise HTTPException(status_code=400, detail="Query did not contain searchable terms")
    return " ".join(f'"{token}"' for token in tokens[:12])


def create_connection(db_path: str) -> sqlite3.Connection:
    ensure_parent(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def format_ts(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    device_name TEXT,
    tailscale_name TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    entry_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    recipient_id TEXT,
    visibility TEXT NOT NULL,
    source_uri TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_agent_namespace_source
ON entries(agent_id, namespace, source_id);

CREATE INDEX IF NOT EXISTS idx_entries_namespace
ON entries(namespace);

CREATE INDEX IF NOT EXISTS idx_entries_kind
ON entries(kind);

CREATE INDEX IF NOT EXISTS idx_entries_recipient
ON entries(recipient_id);

CREATE TABLE IF NOT EXISTS entry_chunks (
    chunk_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    FOREIGN KEY(entry_id) REFERENCES entries(entry_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entry_chunks_entry
ON entry_chunks(entry_id, chunk_index);

CREATE VIRTUAL TABLE IF NOT EXISTS entry_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    entry_id UNINDEXED,
    agent_id,
    namespace,
    kind,
    title,
    tags,
    content,
    tokenize = 'unicode61'
);
"""


class AgentRegistration(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    device_name: str | None = Field(default=None, max_length=200)
    tailscale_name: str | None = Field(default=None, max_length=200)
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntryUpsertRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    namespace: str = Field(default="default", min_length=1, max_length=128)
    source_id: str | None = Field(default=None, max_length=200)
    kind: Literal["note", "artifact", "message"] = "note"
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    recipient_id: str | None = Field(default=None, max_length=128)
    visibility: Literal["shared", "private", "direct", "broadcast"] | None = None
    source_uri: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_size: int = Field(default=900, ge=300, le=4000)
    overlap: int = Field(default=120, ge=0, le=500)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    namespace: str | None = Field(default=None, max_length=128)
    requester_agent_id: str | None = Field(default=None, max_length=128)
    agent_id: str | None = Field(default=None, max_length=128)
    kind: Literal["note", "artifact", "message"] | None = None
    tag: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=8, ge=1, le=50)


def create_app(db_path: str | None = None, shared_key: str | None = None) -> FastAPI:
    app = FastAPI(title="Agent Memory Service", version="0.1.0")
    resolved_db_path = db_path or os.getenv("AGENT_MEMORY_DB_PATH", "/data/agent_memory.db")
    app.state.db_path = resolved_db_path
    app.state.shared_key = shared_key if shared_key is not None else os.getenv("AGENT_MEMORY_SHARED_KEY", "")

    conn = create_connection(resolved_db_path)
    conn.executescript(SCHEMA)
    conn.close()

    def require_key(x_shared_key: str | None = Header(default=None)) -> None:
        expected = app.state.shared_key
        if not expected:
            return
        if x_shared_key != expected:
            raise HTTPException(status_code=401, detail="Invalid shared key")

    def check_key_value(header_value: str | None = None, query_value: str | None = None) -> None:
        expected = app.state.shared_key
        if not expected:
            return
        if header_value == expected or query_value == expected:
            return
        raise HTTPException(status_code=401, detail="Invalid shared key")

    def open_db() -> sqlite3.Connection:
        conn = create_connection(app.state.db_path)
        conn.executescript(SCHEMA)
        return conn

    def dashboard_data(search_query: str | None = None, namespace: str | None = None) -> dict[str, Any]:
        conn = open_db()
        try:
            agent_count = conn.execute("SELECT COUNT(*) AS count FROM agents").fetchone()["count"]
            entry_count = conn.execute("SELECT COUNT(*) AS count FROM entries").fetchone()["count"]
            message_count = conn.execute(
                "SELECT COUNT(*) AS count FROM entries WHERE kind = 'message'"
            ).fetchone()["count"]
            namespace_count = conn.execute(
                "SELECT COUNT(DISTINCT namespace) AS count FROM entries"
            ).fetchone()["count"]
            last_updated = conn.execute(
                "SELECT MAX(updated_at) AS value FROM entries"
            ).fetchone()["value"]

            recent_agents = conn.execute(
                """
                SELECT agent_id, display_name, device_name, tailscale_name, capabilities_json, last_seen_at
                FROM agents
                ORDER BY last_seen_at DESC
                LIMIT 8
                """
            ).fetchall()

            recent_entries_params: list[Any] = []
            recent_entries_where = []
            if namespace:
                recent_entries_where.append("namespace = ?")
                recent_entries_params.append(namespace)
            recent_entries_sql = """
                SELECT entry_id, agent_id, namespace, kind, title, visibility, updated_at
                FROM entries
            """
            if recent_entries_where:
                recent_entries_sql += " WHERE " + " AND ".join(recent_entries_where)
            recent_entries_sql += " ORDER BY updated_at DESC LIMIT 12"
            recent_entries = conn.execute(recent_entries_sql, recent_entries_params).fetchall()

            namespace_rows = conn.execute(
                """
                SELECT namespace, COUNT(*) AS count
                FROM entries
                GROUP BY namespace
                ORDER BY count DESC, namespace ASC
                LIMIT 10
                """
            ).fetchall()

            search_results: list[sqlite3.Row] = []
            if search_query:
                match_query = normalize_fts_query(search_query)
                clauses = ["entry_chunks_fts MATCH ?", "e.visibility IN ('shared', 'broadcast')"]
                params = [match_query]
                if namespace:
                    clauses.append("e.namespace = ?")
                    params.append(namespace)
                params.append(10)
                search_results = conn.execute(
                    f"""
                    SELECT
                        e.entry_id,
                        e.agent_id,
                        e.namespace,
                        e.kind,
                        e.title,
                        e.updated_at,
                        c.content,
                        bm25(entry_chunks_fts, 1.0, 0.5, 0.5, 0.5, 1.2, 1.0, 1.8) AS score
                    FROM entry_chunks_fts
                    JOIN entry_chunks c ON c.chunk_id = entry_chunks_fts.chunk_id
                    JOIN entries e ON e.entry_id = c.entry_id
                    WHERE {" AND ".join(clauses)}
                    ORDER BY score ASC, e.updated_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        finally:
            conn.close()

        return {
            "stats": {
                "agents": agent_count,
                "entries": entry_count,
                "messages": message_count,
                "namespaces": namespace_count,
                "last_updated": last_updated,
            },
            "recent_agents": [
                {
                    "agent_id": row["agent_id"],
                    "display_name": row["display_name"],
                    "device_name": row["device_name"],
                    "tailscale_name": row["tailscale_name"],
                    "capabilities": json.loads(row["capabilities_json"]),
                    "last_seen_at": row["last_seen_at"],
                }
                for row in recent_agents
            ],
            "recent_entries": [
                {
                    "entry_id": row["entry_id"],
                    "agent_id": row["agent_id"],
                    "namespace": row["namespace"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "visibility": row["visibility"],
                    "updated_at": row["updated_at"],
                }
                for row in recent_entries
            ],
            "namespaces": [
                {"namespace": row["namespace"], "count": row["count"]}
                for row in namespace_rows
            ],
            "search": {
                "query": search_query or "",
                "namespace": namespace or "",
                "results": [
                    {
                        "entry_id": row["entry_id"],
                        "agent_id": row["agent_id"],
                        "namespace": row["namespace"],
                        "kind": row["kind"],
                        "title": row["title"],
                        "updated_at": row["updated_at"],
                        "content": row["content"],
                        "score": row["score"],
                    }
                    for row in search_results
                ],
            },
        }

    def render_dashboard(payload: dict[str, Any]) -> str:
        stats = payload["stats"]
        agents_html = "".join(
            (
                "<tr>"
                f"<td>{escape(agent['display_name'])}</td>"
                f"<td><code>{escape(agent['agent_id'])}</code></td>"
                f"<td>{escape(agent['device_name'] or '-')}</td>"
                f"<td>{escape(agent['tailscale_name'] or '-')}</td>"
                f"<td>{escape(', '.join(agent['capabilities']) or '-')}</td>"
                f"<td>{escape(format_ts(agent['last_seen_at']))}</td>"
                "</tr>"
            )
            for agent in payload["recent_agents"]
        ) or "<tr><td colspan='6'>No agents registered yet.</td></tr>"

        entries_html = "".join(
            (
                "<tr>"
                f"<td><code>{escape(entry['namespace'])}</code></td>"
                f"<td>{escape(entry['kind'])}</td>"
                f"<td>{escape(entry['title'])}</td>"
                f"<td><code>{escape(entry['agent_id'])}</code></td>"
                f"<td>{escape(entry['visibility'])}</td>"
                f"<td>{escape(format_ts(entry['updated_at']))}</td>"
                "</tr>"
            )
            for entry in payload["recent_entries"]
        ) or "<tr><td colspan='6'>No entries stored yet.</td></tr>"

        namespaces_html = "".join(
            (
                "<tr>"
                f"<td><code>{escape(item['namespace'])}</code></td>"
                f"<td>{item['count']}</td>"
                "</tr>"
            )
            for item in payload["namespaces"]
        ) or "<tr><td colspan='2'>No namespaces yet.</td></tr>"

        search_results_html = "".join(
            (
                "<article class='search-result'>"
                f"<h3>{escape(item['title'])}</h3>"
                f"<p class='meta'><code>{escape(item['namespace'])}</code> · "
                f"{escape(item['kind'])} · <code>{escape(item['agent_id'])}</code> · "
                f"{escape(format_ts(item['updated_at']))}</p>"
                f"<pre>{escape(item['content'])}</pre>"
                "</article>"
            )
            for item in payload["search"]["results"]
        ) or "<p class='empty'>No search results.</p>"

        query_value = escape(payload["search"]["query"])
        namespace_value = escape(payload["search"]["namespace"])
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Memory Dashboard</title>
  <style>
    :root {{
      --bg: #f3efe6;
      --card: rgba(255,255,255,0.85);
      --ink: #1f1c18;
      --muted: #5f5a53;
      --accent: #0f766e;
      --border: rgba(31,28,24,0.12);
      --shadow: 0 18px 50px rgba(31,28,24,0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.15), transparent 28%),
        radial-gradient(circle at top right, rgba(190,24,93,0.12), transparent 25%),
        linear-gradient(180deg, #f7f4ed 0%, var(--bg) 100%);
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    .hero {{
      display: grid;
      gap: 14px;
      margin-bottom: 24px;
    }}
    .eyebrow {{
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(32px, 6vw, 54px);
      line-height: 0.95;
      max-width: 10ch;
    }}
    .sub {{
      max-width: 68ch;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.6;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin: 24px 0;
    }}
    .card {{
      background: var(--card);
      backdrop-filter: blur(8px);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }}
    .stat {{
      padding: 18px;
    }}
    .stat .label {{
      color: var(--muted);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .stat .value {{
      font-size: 34px;
      font-weight: 700;
      margin-top: 8px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.3fr 0.9fr;
      gap: 18px;
    }}
    .panel {{
      padding: 18px;
      overflow: hidden;
    }}
    .panel h2 {{
      margin: 0 0 14px;
      font-size: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    code {{
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
      font-size: 12px;
      background: rgba(15,118,110,0.08);
      padding: 2px 6px;
      border-radius: 999px;
    }}
    .search-form {{
      display: grid;
      grid-template-columns: 1.6fr 0.9fr auto;
      gap: 10px;
      margin-bottom: 16px;
    }}
    input, button {{
      width: 100%;
      border-radius: 12px;
      border: 1px solid var(--border);
      padding: 12px 14px;
      font: inherit;
      background: rgba(255,255,255,0.92);
    }}
    button {{
      background: var(--accent);
      color: white;
      border: none;
      cursor: pointer;
      font-weight: 700;
    }}
    .search-result {{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      background: rgba(255,255,255,0.75);
      margin-bottom: 12px;
    }}
    .search-result h3 {{
      margin: 0 0 8px;
      font-size: 16px;
    }}
    .meta {{
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 13px;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
      font-size: 12px;
      line-height: 1.55;
    }}
    .footer {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}
    .empty {{
      color: var(--muted);
    }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .search-form {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <div class="eyebrow">Shared RAG Memory</div>
      <h1>Agent Memory Dashboard</h1>
      <p class="sub">One shared memory store for independent Claude and Codex agents across repos, sessions, and Tailscale-connected devices.</p>
    </section>

    <section class="stats">
      <div class="card stat"><div class="label">Agents</div><div class="value">{stats['agents']}</div></div>
      <div class="card stat"><div class="label">Entries</div><div class="value">{stats['entries']}</div></div>
      <div class="card stat"><div class="label">Messages</div><div class="value">{stats['messages']}</div></div>
      <div class="card stat"><div class="label">Namespaces</div><div class="value">{stats['namespaces']}</div></div>
    </section>

    <div class="grid">
      <section class="card panel">
        <h2>Recent Entries</h2>
        <table>
          <thead>
            <tr><th>Namespace</th><th>Kind</th><th>Title</th><th>Agent</th><th>Visibility</th><th>Updated</th></tr>
          </thead>
          <tbody>{entries_html}</tbody>
        </table>
      </section>

      <section class="card panel">
        <h2>Recent Agents</h2>
        <table>
          <thead>
            <tr><th>Name</th><th>Agent ID</th><th>Device</th><th>Tailscale</th><th>Capabilities</th><th>Seen</th></tr>
          </thead>
          <tbody>{agents_html}</tbody>
        </table>
      </section>
    </div>

    <div class="grid" style="margin-top: 18px;">
      <section class="card panel">
        <h2>Search Memory</h2>
        <form class="search-form" method="get" action="/">
          <input type="text" name="q" placeholder="Search runs, files, ports, tools, domains..." value="{query_value}">
          <input type="text" name="namespace" placeholder="Optional namespace" value="{namespace_value}">
          <button type="submit">Search</button>
        </form>
        {search_results_html}
      </section>

      <section class="card panel">
        <h2>Namespaces</h2>
        <table>
          <thead><tr><th>Namespace</th><th>Entries</th></tr></thead>
          <tbody>{namespaces_html}</tbody>
        </table>
        <p class="footer">Last updated: {escape(format_ts(stats['last_updated']))}</p>
      </section>
    </div>
  </main>
</body>
</html>"""

    def upsert_chunks(
        conn: sqlite3.Connection,
        *,
        entry_id: str,
        agent_id: str,
        namespace: str,
        kind: str,
        title: str,
        tags: list[str],
        content: str,
        chunk_size: int,
        overlap: int,
    ) -> int:
        conn.execute("DELETE FROM entry_chunks_fts WHERE entry_id = ?", (entry_id,))
        conn.execute("DELETE FROM entry_chunks WHERE entry_id = ?", (entry_id,))
        chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
        tag_text = " ".join(tags)
        for index, chunk in enumerate(chunks):
            chunk_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO entry_chunks (chunk_id, entry_id, chunk_index, content)
                VALUES (?, ?, ?, ?)
                """,
                (chunk_id, entry_id, index, chunk),
            )
            conn.execute(
                """
                INSERT INTO entry_chunks_fts (chunk_id, entry_id, agent_id, namespace, kind, title, tags, content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, entry_id, agent_id, namespace, kind, title, tag_text, chunk),
            )
        return len(chunks)

    @app.get("/health")
    def health(_: None = Depends(require_key)) -> dict[str, Any]:
        return {"status": "ok", "db_path": app.state.db_path}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        q: str | None = Query(default=None),
        namespace: str | None = Query(default=None),
        key: str | None = Query(default=None),
        x_shared_key: str | None = Header(default=None),
    ) -> HTMLResponse:
        check_key_value(x_shared_key, key)
        payload = dashboard_data(search_query=q, namespace=namespace)
        return HTMLResponse(render_dashboard(payload))

    @app.get("/dashboard/data")
    def dashboard_json(
        q: str | None = Query(default=None),
        namespace: str | None = Query(default=None),
        key: str | None = Query(default=None),
        x_shared_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        check_key_value(x_shared_key, key)
        return dashboard_data(search_query=q, namespace=namespace)

    @app.post("/v1/agents/register")
    def register_agent(payload: AgentRegistration, _: None = Depends(require_key)) -> dict[str, Any]:
        now = utc_now()
        conn = open_db()
        try:
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, display_name, device_name, tailscale_name, capabilities_json, metadata_json, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    device_name = excluded.device_name,
                    tailscale_name = excluded.tailscale_name,
                    capabilities_json = excluded.capabilities_json,
                    metadata_json = excluded.metadata_json,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    payload.agent_id,
                    payload.display_name,
                    payload.device_name,
                    payload.tailscale_name,
                    json.dumps(payload.capabilities),
                    json.dumps(payload.metadata),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {"status": "registered", "agent_id": payload.agent_id, "last_seen_at": now}

    @app.get("/v1/agents")
    def list_agents(
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(require_key),
    ) -> dict[str, Any]:
        conn = open_db()
        try:
            rows = conn.execute(
                """
                SELECT agent_id, display_name, device_name, tailscale_name, capabilities_json, metadata_json, last_seen_at
                FROM agents
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        agents = []
        for row in rows:
            agents.append(
                {
                    "agent_id": row["agent_id"],
                    "display_name": row["display_name"],
                    "device_name": row["device_name"],
                    "tailscale_name": row["tailscale_name"],
                    "capabilities": json.loads(row["capabilities_json"]),
                    "metadata": json.loads(row["metadata_json"]),
                    "last_seen_at": row["last_seen_at"],
                }
            )
        return {"agents": agents}

    @app.post("/v1/entries/upsert")
    def upsert_entry(payload: EntryUpsertRequest, _: None = Depends(require_key)) -> dict[str, Any]:
        now = utc_now()
        source_id = payload.source_id or str(uuid.uuid4())
        tags = normalize_tags(payload.tags)
        if payload.kind == "message":
            visibility = "direct" if payload.recipient_id else "broadcast"
        else:
            visibility = payload.visibility or "shared"

        conn = open_db()
        try:
            agent_exists = conn.execute(
                "SELECT 1 FROM agents WHERE agent_id = ?",
                (payload.agent_id,),
            ).fetchone()
            if not agent_exists:
                raise HTTPException(status_code=404, detail="Agent must register before publishing entries")

            existing = conn.execute(
                """
                SELECT entry_id, created_at
                FROM entries
                WHERE agent_id = ? AND namespace = ? AND source_id = ?
                """,
                (payload.agent_id, payload.namespace, source_id),
            ).fetchone()

            entry_id = existing["entry_id"] if existing else str(uuid.uuid4())
            created_at = existing["created_at"] if existing else now

            conn.execute(
                """
                INSERT INTO entries (
                    entry_id, agent_id, namespace, source_id, kind, title, content, tags_json,
                    recipient_id, visibility, source_uri, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    content = excluded.content,
                    tags_json = excluded.tags_json,
                    recipient_id = excluded.recipient_id,
                    visibility = excluded.visibility,
                    source_uri = excluded.source_uri,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    entry_id,
                    payload.agent_id,
                    payload.namespace,
                    source_id,
                    payload.kind,
                    payload.title,
                    payload.content,
                    json.dumps(tags),
                    payload.recipient_id,
                    visibility,
                    payload.source_uri,
                    json.dumps(payload.metadata),
                    created_at,
                    now,
                ),
            )
            chunk_count = upsert_chunks(
                conn,
                entry_id=entry_id,
                agent_id=payload.agent_id,
                namespace=payload.namespace,
                kind=payload.kind,
                title=payload.title,
                tags=tags,
                content=payload.content,
                chunk_size=payload.chunk_size,
                overlap=payload.overlap,
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "status": "stored",
            "entry_id": entry_id,
            "source_id": source_id,
            "chunk_count": chunk_count,
            "visibility": visibility,
        }

    @app.post("/v1/search")
    def search(payload: SearchRequest, _: None = Depends(require_key)) -> dict[str, Any]:
        match_query = normalize_fts_query(payload.query)
        clauses = ["entry_chunks_fts MATCH ?"]
        params: list[Any] = [match_query]

        if payload.namespace:
            clauses.append("e.namespace = ?")
            params.append(payload.namespace)
        if payload.agent_id:
            clauses.append("e.agent_id = ?")
            params.append(payload.agent_id)
        if payload.kind:
            clauses.append("e.kind = ?")
            params.append(payload.kind)
        if payload.tag:
            clauses.append("e.tags_json LIKE ?")
            params.append(f'%"{payload.tag.strip().lower()}"%')

        if payload.requester_agent_id:
            clauses.append(
                "(e.visibility IN ('shared', 'broadcast') OR e.agent_id = ? OR e.recipient_id = ?)"
            )
            params.extend([payload.requester_agent_id, payload.requester_agent_id])
        else:
            clauses.append("e.visibility IN ('shared', 'broadcast')")

        params.append(payload.limit)

        sql = f"""
            SELECT
                e.entry_id,
                e.agent_id,
                e.namespace,
                e.kind,
                e.title,
                e.tags_json,
                e.recipient_id,
                e.visibility,
                e.source_uri,
                e.metadata_json,
                e.updated_at,
                c.chunk_id,
                c.chunk_index,
                c.content,
                bm25(entry_chunks_fts, 1.0, 0.5, 0.5, 0.5, 1.2, 1.0, 1.8) AS score
            FROM entry_chunks_fts
            JOIN entry_chunks c ON c.chunk_id = entry_chunks_fts.chunk_id
            JOIN entries e ON e.entry_id = c.entry_id
            WHERE {" AND ".join(clauses)}
            ORDER BY score ASC, e.updated_at DESC
            LIMIT ?
        """

        conn = open_db()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        results = []
        for row in rows:
            results.append(
                {
                    "entry_id": row["entry_id"],
                    "chunk_id": row["chunk_id"],
                    "agent_id": row["agent_id"],
                    "namespace": row["namespace"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "tags": json.loads(row["tags_json"]),
                    "recipient_id": row["recipient_id"],
                    "visibility": row["visibility"],
                    "source_uri": row["source_uri"],
                    "metadata": json.loads(row["metadata_json"]),
                    "updated_at": row["updated_at"],
                    "chunk_index": row["chunk_index"],
                    "content": row["content"],
                    "score": row["score"],
                }
            )
        return {"results": results}

    @app.get("/v1/entries/{entry_id}")
    def get_entry(
        entry_id: str,
        requester_agent_id: str | None = Query(default=None),
        _: None = Depends(require_key),
    ) -> dict[str, Any]:
        conn = open_db()
        try:
            row = conn.execute(
                """
                SELECT entry_id, agent_id, namespace, source_id, kind, title, content, tags_json,
                       recipient_id, visibility, source_uri, metadata_json, created_at, updated_at
                FROM entries
                WHERE entry_id = ?
                """,
                (entry_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Entry not found")
            if row["visibility"] not in {"shared", "broadcast"}:
                if requester_agent_id not in {row["agent_id"], row["recipient_id"]}:
                    raise HTTPException(status_code=403, detail="Entry is not visible to requester")
            chunks = conn.execute(
                """
                SELECT chunk_id, chunk_index, content
                FROM entry_chunks
                WHERE entry_id = ?
                ORDER BY chunk_index ASC
                """,
                (entry_id,),
            ).fetchall()
        finally:
            conn.close()

        return {
            "entry": {
                "entry_id": row["entry_id"],
                "agent_id": row["agent_id"],
                "namespace": row["namespace"],
                "source_id": row["source_id"],
                "kind": row["kind"],
                "title": row["title"],
                "content": row["content"],
                "tags": json.loads(row["tags_json"]),
                "recipient_id": row["recipient_id"],
                "visibility": row["visibility"],
                "source_uri": row["source_uri"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "chunks": [
                    {
                        "chunk_id": chunk["chunk_id"],
                        "chunk_index": chunk["chunk_index"],
                        "content": chunk["content"],
                    }
                    for chunk in chunks
                ],
            }
        }

    @app.get("/v1/messages/inbox/{agent_id}")
    def inbox(
        agent_id: str,
        namespace: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        _: None = Depends(require_key),
    ) -> dict[str, Any]:
        clauses = ["kind = 'message'", "(recipient_id = ? OR visibility = 'broadcast')"]
        params: list[Any] = [agent_id]
        if namespace:
            clauses.append("namespace = ?")
            params.append(namespace)
        params.append(limit)

        conn = open_db()
        try:
            rows = conn.execute(
                f"""
                SELECT entry_id, agent_id, namespace, source_id, title, content, tags_json, recipient_id,
                       visibility, source_uri, metadata_json, created_at, updated_at
                FROM entries
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        messages = []
        for row in rows:
            messages.append(
                {
                    "entry_id": row["entry_id"],
                    "sender_agent_id": row["agent_id"],
                    "namespace": row["namespace"],
                    "source_id": row["source_id"],
                    "title": row["title"],
                    "content": row["content"],
                    "tags": json.loads(row["tags_json"]),
                    "recipient_id": row["recipient_id"],
                    "visibility": row["visibility"],
                    "source_uri": row["source_uri"],
                    "metadata": json.loads(row["metadata_json"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return {"messages": messages}

    return app


app = create_app()
