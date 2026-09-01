"""Entry upsert, fetch, delete, and decision recording."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from ..context import AppContext
from ..models import DecisionRequest, EntryUpsertRequest
from ..projects import GLOBAL_PROJECT
from ..store import deindex, index_chunks, new_id, upsert_project, utc_now
from ..text import chunk_text, normalize_tags


def _store_entry(ctx: AppContext, conn, payload: EntryUpsertRequest) -> dict[str, Any]:
    now = utc_now()
    source_id = payload.source_id or str(uuid.uuid4())
    tags = normalize_tags(payload.tags)
    if payload.kind == "message":
        visibility = "direct" if payload.recipient_id else "broadcast"
    else:
        visibility = payload.visibility or "shared"

    agent_exists = conn.execute(
        "SELECT 1 FROM agents WHERE agent_id = ?", (payload.agent_id,)
    ).fetchone()
    if not agent_exists:
        raise HTTPException(status_code=404, detail="Agent must register before publishing entries")

    project_key = payload.project_key or GLOBAL_PROJECT
    if project_key != GLOBAL_PROJECT:
        upsert_project(conn, project_key)

    existing = conn.execute(
        "SELECT entry_id, created_at FROM entries WHERE agent_id = ? AND namespace = ? AND source_id = ?",
        (payload.agent_id, payload.namespace, source_id),
    ).fetchone()
    entry_id = existing["entry_id"] if existing else str(uuid.uuid4())
    created_at = existing["created_at"] if existing else now

    metadata = dict(payload.metadata)
    if payload.cwd:
        metadata.setdefault("cwd", payload.cwd)

    conn.execute(
        """
        INSERT INTO entries (
            entry_id, agent_id, namespace, source_id, kind, title, content, tags_json,
            recipient_id, visibility, source_uri, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entry_id) DO UPDATE SET
            kind = excluded.kind, title = excluded.title, content = excluded.content,
            tags_json = excluded.tags_json, recipient_id = excluded.recipient_id,
            visibility = excluded.visibility, source_uri = excluded.source_uri,
            metadata_json = excluded.metadata_json, updated_at = excluded.updated_at
        """,
        (
            entry_id, payload.agent_id, payload.namespace, source_id, payload.kind,
            payload.title, payload.content, json.dumps(tags), payload.recipient_id,
            visibility, payload.source_uri, json.dumps(metadata), created_at, now,
        ),
    )

    # Canonical chunk storage (served by get_entry) ...
    conn.execute("DELETE FROM entry_chunks WHERE entry_id = ?", (entry_id,))
    pieces = chunk_text(payload.content, chunk_size=payload.chunk_size, overlap=payload.overlap)
    chunk_pairs: list[tuple[str, str]] = []
    for index, piece in enumerate(pieces):
        chunk_id = new_id()
        conn.execute(
            "INSERT INTO entry_chunks (chunk_id, entry_id, chunk_index, content) VALUES (?, ?, ?, ?)",
            (chunk_id, entry_id, index, piece),
        )
        chunk_pairs.append((chunk_id, piece))

    # ... and the unified retrieval index, sharing the same chunk ids.
    index_chunks(
        conn,
        source_type="entry",
        source_ref=entry_id,
        project_key=project_key,
        title=payload.title,
        chunks=chunk_pairs,
        kind=payload.kind,
        namespace=payload.namespace,
        agent_id=payload.agent_id,
        tags=tags,
        visibility=visibility,
        recipient_id=payload.recipient_id,
        ts=now,
    )
    conn.commit()
    return {
        "status": "stored",
        "entry_id": entry_id,
        "source_id": source_id,
        "chunk_count": len(chunk_pairs),
        "visibility": visibility,
        "project_key": project_key,
    }


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.post("/v1/entries/upsert")
    def upsert_entry(
        payload: EntryUpsertRequest, _: None = Depends(ctx.require_key_dep())
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            return _store_entry(ctx, conn, payload)
        finally:
            conn.close()

    @app.post("/v1/decisions")
    def record_decision(
        payload: DecisionRequest, _: None = Depends(ctx.require_key_dep())
    ) -> dict[str, Any]:
        """Pin a durable decision so future sessions respect it."""
        content = payload.content
        if payload.rationale:
            content = f"{content}\n\nRationale: {payload.rationale}"
        if payload.supersedes:
            content = f"{content}\n\nSupersedes: {payload.supersedes}"

        entry = EntryUpsertRequest(
            agent_id=payload.agent_id,
            namespace=payload.namespace,
            source_id=f"decision:{payload.title.lower().replace(' ', '-')[:80]}",
            kind="decision",
            title=payload.title,
            content=content,
            tags=[*payload.tags, "decision"],
            project_key=payload.project_key,
            cwd=payload.cwd,
            metadata={"session_id": payload.session_id} if payload.session_id else {},
        )
        conn = ctx.open()
        try:
            return _store_entry(ctx, conn, entry)
        finally:
            conn.close()

    @app.get("/v1/entries/{entry_id}")
    def get_entry(
        entry_id: str,
        requester_agent_id: str | None = Query(default=None),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            row = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Entry not found")
            if row["visibility"] not in {"shared", "broadcast"}:
                if requester_agent_id not in {row["agent_id"], row["recipient_id"]}:
                    raise HTTPException(status_code=403, detail="Entry is not visible to requester")
            chunks = conn.execute(
                "SELECT chunk_id, chunk_index, content FROM entry_chunks WHERE entry_id = ? ORDER BY chunk_index ASC",
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
                    {"chunk_id": c["chunk_id"], "chunk_index": c["chunk_index"], "content": c["content"]}
                    for c in chunks
                ],
            }
        }

    @app.delete("/v1/entries/{entry_id}")
    def delete_entry(
        entry_id: str,
        requester_agent_id: str | None = Query(default=None),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        """Delete an entry and every index artefact derived from it."""
        conn = ctx.open()
        try:
            row = conn.execute(
                "SELECT agent_id, visibility, recipient_id FROM entries WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Entry not found")
            if row["visibility"] not in {"shared", "broadcast"}:
                if requester_agent_id not in {row["agent_id"], row["recipient_id"]}:
                    raise HTTPException(status_code=403, detail="Entry is not visible to requester")
            deindex(conn, "entry", entry_id)
            conn.execute("DELETE FROM entry_chunks WHERE entry_id = ?", (entry_id,))
            conn.execute("DELETE FROM entries WHERE entry_id = ?", (entry_id,))
            conn.commit()
        finally:
            conn.close()
        return {"status": "deleted", "entry_id": entry_id}
