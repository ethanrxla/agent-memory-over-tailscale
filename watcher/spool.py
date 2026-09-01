"""Local spool: byte-offset checkpoints and an outbound event queue.

Devices on this tailnet go offline for weeks (one has been dark 100 days), so
the watcher must never lose events when the hub is unreachable. Everything the
watcher intends to send is written to a local SQLite spool first; delivery is a
separate step that drains the spool and can be retried indefinitely.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SPOOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    path TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    last_seq INTEGER NOT NULL DEFAULT -1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    delivered INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_outbound_undelivered ON outbound(delivered, id);
"""


class Spool:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(SPOOL_SCHEMA)
        self.conn.commit()

    def checkpoint(self, path: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT byte_offset, last_seq FROM checkpoints WHERE path = ?", (path,)
        ).fetchone()
        return (row["byte_offset"], row["last_seq"]) if row else (0, -1)

    def set_checkpoint(self, path: str, byte_offset: int, last_seq: int) -> None:
        self.conn.execute(
            """
            INSERT INTO checkpoints (path, byte_offset, last_seq, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(path) DO UPDATE SET
                byte_offset = excluded.byte_offset,
                last_seq = excluded.last_seq,
                updated_at = excluded.updated_at
            """,
            (path, byte_offset, last_seq),
        )
        self.conn.commit()

    def enqueue(self, session_id: str, seq: int, payload: dict[str, Any]) -> bool:
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO outbound (session_id, seq, payload) VALUES (?, ?, ?)",
            (session_id, seq, json.dumps(payload)),
        )
        return bool(cursor.rowcount)

    def commit(self) -> None:
        self.conn.commit()

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM outbound WHERE delivered = 0").fetchone()["c"]

    def next_batch(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, session_id, seq, payload FROM outbound WHERE delivered = 0 ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()

    def mark_delivered(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(f"UPDATE outbound SET delivered = 1 WHERE id IN ({placeholders})", ids)
        self.conn.commit()

    def prune_delivered(self, keep_recent: int = 500) -> int:
        cursor = self.conn.execute(
            """
            DELETE FROM outbound WHERE delivered = 1 AND id NOT IN (
                SELECT id FROM outbound WHERE delivered = 1 ORDER BY id DESC LIMIT ?
            )
            """,
            (keep_recent,),
        )
        self.conn.commit()
        return cursor.rowcount or 0

    def close(self) -> None:
        self.conn.close()
