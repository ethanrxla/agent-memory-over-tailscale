"""Background worker: drains the embedding queue and refreshes summaries.

Runs as a daemon thread inside the hub process. Every operation is safe to
interrupt: work is tracked in SQLite, so a restart resumes rather than loses.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .db import VECTOR_TABLE, create_connection, ensure_vector_table, serialize_vector, sqlite_vec_available
from .nim import NimClient
from .store import mark_idle_sessions, utc_now
from .summarize import generate_summary

log = logging.getLogger("agent_memory.worker")

MAX_ATTEMPTS = 6


def _backoff_at(attempts: int) -> str:
    delay = min(3600, 30 * (2 ** max(0, attempts - 1)))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def drain_embeddings(conn: sqlite3.Connection, config: Config, nim: NimClient) -> dict[str, int]:
    """Embed one batch of queued chunks. Returns counts; never raises."""
    stats = {"embedded": 0, "failed": 0, "skipped": 0}
    if not nim.enabled or not sqlite_vec_available():
        return stats

    ensure_vector_table(conn, config.embed_dim)
    now = utc_now()
    rows = conn.execute(
        """
        SELECT q.chunk_id, q.attempts, c.content, c.project_key
        FROM embedding_queue q
        JOIN memory_chunks c ON c.chunk_id = q.chunk_id
        WHERE q.next_attempt_at <= ? AND q.attempts < ?
        ORDER BY q.enqueued_at ASC
        LIMIT ?
        """,
        (now, MAX_ATTEMPTS, config.embed_batch),
    ).fetchall()
    if not rows:
        return stats

    texts = [row["content"] for row in rows]
    vectors = nim.embed(texts, input_type="passage")

    if vectors is None:
        error = (nim.last_error or "embedding failed")[:400]
        for row in rows:
            attempts = row["attempts"] + 1
            conn.execute(
                "UPDATE embedding_queue SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE chunk_id = ?",
                (attempts, _backoff_at(attempts), error, row["chunk_id"]),
            )
        conn.commit()
        stats["failed"] = len(rows)
        return stats

    for row, vector in zip(rows, vectors):
        if len(vector) != config.embed_dim:
            # Model returned a different width than configured; stop rather
            # than write vectors that can never be compared correctly.
            conn.execute(
                "UPDATE embedding_queue SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE chunk_id = ?",
                (MAX_ATTEMPTS, _backoff_at(MAX_ATTEMPTS), f"dim mismatch: got {len(vector)}", row["chunk_id"]),
            )
            stats["skipped"] += 1
            continue
        blob = serialize_vector(vector)
        try:
            conn.execute(f"DELETE FROM {VECTOR_TABLE} WHERE chunk_id = ?", (row["chunk_id"],))
            conn.execute(
                f"INSERT INTO {VECTOR_TABLE} (chunk_id, project_key, embedding) VALUES (?, ?, ?)",
                (row["chunk_id"], row["project_key"], blob),
            )
        except sqlite3.OperationalError as exc:
            log.warning("vector insert failed for %s: %s", row["chunk_id"], exc)
            stats["failed"] += 1
            continue
        conn.execute(
            "UPDATE memory_chunks SET embed_model = ?, embedded_at = ? WHERE chunk_id = ?",
            (config.embed_model, utc_now(), row["chunk_id"]),
        )
        conn.execute("DELETE FROM embedding_queue WHERE chunk_id = ?", (row["chunk_id"],))
        stats["embedded"] += 1

    conn.commit()
    return stats


def drain_summaries(conn: sqlite3.Connection, config: Config, nim: NimClient, limit: int = 3) -> dict[str, int]:
    """Refresh rolling summaries and write finals for sessions that went idle."""
    stats = {"rolling": 0, "final": 0}
    mark_idle_sessions(conn, config.session_idle_minutes)
    conn.commit()

    finals = conn.execute(
        """
        SELECT session_id FROM sessions
        WHERE status IN ('idle', 'closed')
          AND event_count > 0
          AND (summary_dirty = 1 OR session_id NOT IN (
                SELECT session_id FROM session_summaries WHERE tier = 'final'))
        ORDER BY last_event_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in finals:
        if generate_summary(conn, config, nim, row["session_id"], tier="final"):
            stats["final"] += 1
            conn.commit()

    rolling = conn.execute(
        """
        SELECT session_id FROM sessions
        WHERE status = 'active'
          AND summary_dirty = 1
          AND event_count > 0
          AND event_count - summarized_event_count >= ?
        ORDER BY last_event_at DESC
        LIMIT ?
        """,
        (max(1, config.rolling_summary_every), limit),
    ).fetchall()
    for row in rolling:
        if generate_summary(conn, config, nim, row["session_id"], tier="rolling"):
            stats["rolling"] += 1
            conn.commit()

    return stats


def sweep_retention(conn: sqlite3.Connection, config: Config) -> int:
    """Delete session events older than the retention window (0 disables).

    Summaries and indexed chunks are kept -- only the bulky raw event log ages
    out, so old sessions stay searchable via their summary at a fraction of the
    storage.
    """
    if config.retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.retention_days)).isoformat()
    cursor = conn.execute("DELETE FROM session_events WHERE ts < ?", (cutoff,))
    if cursor.rowcount:
        conn.execute(
            "UPDATE sessions SET event_count = (SELECT COUNT(*) FROM session_events WHERE session_id = sessions.session_id)"
        )
    return cursor.rowcount or 0


def run_once(config: Config, nim: NimClient) -> dict[str, Any]:
    conn = create_connection(config.db_path)
    try:
        summaries = drain_summaries(conn, config, nim)
        embeddings = drain_embeddings(conn, config, nim)
        purged = sweep_retention(conn, config)
        conn.commit()
    finally:
        conn.close()
    return {"summaries": summaries, "embeddings": embeddings, "purged_events": purged}


class BackgroundWorker:
    def __init__(self, config: Config, nim: NimClient) -> None:
        self.config = config
        self.nim = nim
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.runs = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="agent-memory-worker", daemon=True)
        self._thread.start()
        log.info("background worker started (interval=%ss)", self.config.worker_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_run = run_once(self.config, self.nim)
                self.last_error = None
            except Exception as exc:  # pragma: no cover - defensive
                self.last_error = str(exc)
                log.warning("worker cycle failed: %s", exc)
            self.runs += 1
            self._stop.wait(self.config.worker_interval)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "runs": self.runs,
            "last_run": self.last_run,
            "last_error": self.last_error,
            "nim_enabled": self.nim.enabled,
            "nim_last_error": self.nim.last_error,
        }
