"""Data access: entries, the unified index, sessions, events, and scoping."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .db import GLOBAL_PARTITION, VECTOR_TABLE, sqlite_vec_available
from .text import chunk_text, estimate_tokens, normalize_tags


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_ts(value: str | None) -> str:
    if not value:
        return "-"
    parsed = parse_ts(value)
    if parsed is None:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------------------------------------------------------
# Unified index
# --------------------------------------------------------------------------


def deindex(conn: sqlite3.Connection, source_type: str, source_ref: str) -> None:
    """Remove every index artefact for one source (chunks, FTS, vectors, queue)."""
    rows = conn.execute(
        "SELECT chunk_id FROM memory_chunks WHERE source_type = ? AND source_ref = ?",
        (source_type, source_ref),
    ).fetchall()
    if not rows:
        return
    ids = [row["chunk_id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM memory_chunks_fts WHERE chunk_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM embedding_queue WHERE chunk_id IN ({placeholders})", ids)
    if sqlite_vec_available():
        try:
            conn.execute(f"DELETE FROM {VECTOR_TABLE} WHERE chunk_id IN ({placeholders})", ids)
        except sqlite3.OperationalError:
            pass
    conn.execute(f"DELETE FROM memory_chunks WHERE chunk_id IN ({placeholders})", ids)


def index_chunks(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_ref: str,
    project_key: str | None,
    title: str,
    chunks: Iterable[tuple[str, str]],
    kind: str = "note",
    namespace: str = "default",
    agent_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    visibility: str = "shared",
    recipient_id: str | None = None,
    is_sidechain: bool = False,
    ts: str | None = None,
    replace: bool = True,
) -> int:
    """Write chunks into the unified index and queue them for embedding.

    ``chunks`` yields (chunk_id, content). Deterministic chunk ids make
    re-indexing idempotent.
    """
    if replace:
        deindex(conn, source_type, source_ref)

    now = ts or utc_now()
    tag_text = " ".join(tags or [])
    partition = project_key or GLOBAL_PARTITION
    count = 0

    for index, (chunk_id, content) in enumerate(chunks):
        if not content.strip():
            continue
        conn.execute(
            """
            INSERT INTO memory_chunks (
                chunk_id, source_type, source_ref, project_key, namespace, agent_id,
                session_id, kind, title, content, tags, visibility, recipient_id,
                is_sidechain, chunk_index, ts, embed_model, embedded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(chunk_id) DO UPDATE SET
                content = excluded.content,
                title = excluded.title,
                tags = excluded.tags,
                project_key = excluded.project_key,
                ts = excluded.ts,
                embed_model = NULL,
                embedded_at = NULL
            """,
            (
                chunk_id, source_type, source_ref, partition, namespace, agent_id,
                session_id, kind, title, content, tag_text, visibility, recipient_id,
                1 if is_sidechain else 0, index, now,
            ),
        )
        conn.execute("DELETE FROM memory_chunks_fts WHERE chunk_id = ?", (chunk_id,))
        conn.execute(
            "INSERT INTO memory_chunks_fts (chunk_id, title, tags, content) VALUES (?, ?, ?, ?)",
            (chunk_id, title, tag_text, content),
        )
        conn.execute(
            """
            INSERT INTO embedding_queue (chunk_id, enqueued_at, attempts, next_attempt_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                enqueued_at = excluded.enqueued_at,
                attempts = 0,
                next_attempt_at = excluded.next_attempt_at,
                last_error = NULL
            """,
            (chunk_id, now, now),
        )
        count += 1
    return count


# --------------------------------------------------------------------------
# Projects and scope
# --------------------------------------------------------------------------


def upsert_project(
    conn: sqlite3.Connection,
    project_key: str,
    *,
    display_name: str | None = None,
    repo_root: str | None = None,
    git_remote: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO projects (project_key, display_name, repo_root, git_remote, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_key) DO UPDATE SET
            display_name = COALESCE(excluded.display_name, projects.display_name),
            repo_root = COALESCE(excluded.repo_root, projects.repo_root),
            git_remote = COALESCE(excluded.git_remote, projects.git_remote),
            updated_at = excluded.updated_at
        """,
        (
            project_key,
            display_name or project_key.rsplit("/", 1)[-1],
            repo_root,
            git_remote,
            now,
            now,
        ),
    )


def resolve_scope(conn: sqlite3.Connection, project_key: str | None, scope: str) -> list[str] | None:
    """Return the project keys a query may read, or None for unrestricted.

    This is relevance-isolation layer 1. ``project`` (the default) confines a
    query to exactly one project; ``linked`` adds only explicitly linked
    projects; ``global`` opts out entirely and must be asked for by name.
    """
    if scope == "global" or not project_key:
        return None
    keys = [project_key]
    if scope == "linked":
        rows = conn.execute(
            "SELECT linked_project_key FROM project_links WHERE project_key = ?",
            (project_key,),
        ).fetchall()
        keys.extend(row["linked_project_key"] for row in rows)
    return list(dict.fromkeys(keys))


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def upsert_session(conn: sqlite3.Connection, payload: Any) -> str:
    now = utc_now()
    upsert_project(
        conn,
        payload.project_key,
        display_name=payload.display_name,
        repo_root=payload.repo_root,
        git_remote=payload.git_remote,
    )
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, project_key, agent_id, device_name, tool, cwd, git_branch,
            ai_title, last_prompt, status, is_sidechain, started_at, last_event_at,
            event_count, token_estimate, summary_dirty, summarized_event_count, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, 0, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            project_key = excluded.project_key,
            agent_id = COALESCE(excluded.agent_id, sessions.agent_id),
            device_name = COALESCE(excluded.device_name, sessions.device_name),
            tool = CASE WHEN excluded.tool = 'unknown' THEN sessions.tool ELSE excluded.tool END,
            cwd = COALESCE(excluded.cwd, sessions.cwd),
            git_branch = COALESCE(excluded.git_branch, sessions.git_branch),
            ai_title = COALESCE(excluded.ai_title, sessions.ai_title),
            last_prompt = COALESCE(excluded.last_prompt, sessions.last_prompt),
            status = COALESCE(excluded.status, sessions.status),
            metadata_json = excluded.metadata_json
        """,
        (
            payload.session_id,
            payload.project_key,
            payload.agent_id,
            payload.device_name,
            payload.tool,
            payload.cwd,
            payload.git_branch,
            payload.ai_title,
            payload.last_prompt,
            payload.status or "active",
            1 if payload.is_sidechain else 0,
            payload.started_at or now,
            now,
            json.dumps(payload.metadata),
        ),
    )
    return payload.session_id


def insert_events(conn: sqlite3.Connection, session_id: str, events: list[Any]) -> dict[str, int]:
    """Insert events idempotently, keyed on (session_id, seq).

    A watcher replaying its offline spool re-sends events it already delivered;
    those collide on the unique index and are ignored rather than duplicated.
    """
    inserted = 0
    skipped = 0
    latest_ts: str | None = None
    tokens = 0

    for event in events:
        ts = event.ts or utc_now()
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO session_events (
                event_id, session_id, seq, ts, role, kind, content,
                files_json, commands_json, is_sidechain, token_estimate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{session_id}:{event.seq}",
                session_id,
                event.seq,
                ts,
                event.role,
                event.kind,
                event.content,
                json.dumps(event.files),
                json.dumps(event.commands),
                1 if event.is_sidechain else 0,
                estimate_tokens(event.content),
            ),
        )
        if cursor.rowcount:
            inserted += 1
            tokens += estimate_tokens(event.content)
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
        else:
            skipped += 1

    if inserted:
        conn.execute(
            """
            UPDATE sessions
            SET event_count = (SELECT COUNT(*) FROM session_events WHERE session_id = ?),
                token_estimate = token_estimate + ?,
                last_event_at = MAX(last_event_at, ?),
                status = CASE WHEN status = 'closed' THEN 'active' ELSE status END,
                summary_dirty = 1
            WHERE session_id = ?
            """,
            (session_id, tokens, latest_ts or utc_now(), session_id),
        )

    return {"inserted": inserted, "skipped": skipped}


def mark_idle_sessions(conn: sqlite3.Connection, idle_minutes: int) -> int:
    """Move sessions with no recent events to 'idle' so they get a final summary."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)).isoformat()
    cursor = conn.execute(
        "UPDATE sessions SET status = 'idle' WHERE status = 'active' AND last_event_at < ?",
        (cutoff,),
    )
    return cursor.rowcount or 0


def new_id() -> str:
    return str(uuid.uuid4())
