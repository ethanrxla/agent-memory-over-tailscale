import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
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

    def open_db() -> sqlite3.Connection:
        conn = create_connection(app.state.db_path)
        conn.executescript(SCHEMA)
        return conn

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
