"""SQLite connection management, migrations, and optional sqlite-vec loading."""

from __future__ import annotations

import sqlite3
import threading
from importlib import resources
from pathlib import Path

_VEC_STATE: dict[str, object] = {"checked": False, "available": False, "error": None}
_VEC_LOCK = threading.Lock()


def ensure_parent(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _probe_sqlite_vec() -> tuple[bool, str | None]:
    """Detect sqlite-vec once per process.

    Returns (available, error). Never raises: vector search is an enhancement
    and the hub must stay fully functional on FTS5 alone.
    """
    try:
        import sqlite_vec  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on install
        return False, f"sqlite-vec not installed: {exc}"

    probe = sqlite3.connect(":memory:")
    try:
        probe.enable_load_extension(True)
        sqlite_vec.load(probe)
        probe.enable_load_extension(False)
        probe.execute("CREATE VIRTUAL TABLE _probe USING vec0(embedding float[4])")
        return True, None
    except Exception as exc:  # pragma: no cover - depends on platform
        return False, f"sqlite-vec failed to load: {exc}"
    finally:
        probe.close()


def sqlite_vec_available() -> bool:
    with _VEC_LOCK:
        if not _VEC_STATE["checked"]:
            available, error = _probe_sqlite_vec()
            _VEC_STATE.update({"checked": True, "available": available, "error": error})
    return bool(_VEC_STATE["available"])


def sqlite_vec_error() -> str | None:
    sqlite_vec_available()
    return _VEC_STATE["error"]  # type: ignore[return-value]


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    if not sqlite_vec_available():
        return False
    try:
        import sqlite_vec  # type: ignore

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:  # pragma: no cover - defensive
        return False


def create_connection(db_path: str, *, load_vec: bool = True) -> sqlite3.Connection:
    """Open a tuned connection. Callers own closing it."""
    ensure_parent(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    if load_vec:
        _load_vec_extension(conn)
    return conn


def _migration_files() -> list[tuple[str, str]]:
    """Return (name, sql) ordered by filename."""
    package = resources.files("agent_memory") / "migrations"
    items: list[tuple[str, str]] = []
    for entry in sorted(package.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".sql"):
            items.append((entry.name, entry.read_text(encoding="utf-8")))
    return items


def migrate(db_path: str) -> list[str]:
    """Apply pending migrations. Idempotent; safe to call on every startup.

    Migrations that need sqlite-vec are named ``*.vec.sql`` and are skipped
    (not recorded) when the extension is unavailable, so they get applied
    later if sqlite-vec is installed afterwards.
    """
    conn = create_connection(db_path)
    applied: list[str] = []
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
        done = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        for name, sql in _migration_files():
            if name in done:
                continue
            if name.endswith(".vec.sql") and not sqlite_vec_available():
                continue
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
            conn.commit()
            applied.append(name)
    finally:
        conn.close()
    return applied


VECTOR_TABLE = "memory_vectors"
GLOBAL_PARTITION = "__global__"


def _vector_table_dim(conn: sqlite3.Connection) -> int | None:
    """Return the float[] dimension the existing vector table was built with."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (VECTOR_TABLE,)
    ).fetchone()
    if not row or not row["sql"]:
        return None
    import re

    match = re.search(r"float\[(\d+)\]", row["sql"])
    return int(match.group(1)) if match else None


def ensure_vector_table(conn: sqlite3.Connection, dim: int) -> bool:
    """Create the vec0 table, rebuilding it if the embedding dimension changed.

    ``project_key`` is a vec0 partition key, so project scoping is enforced by
    the index itself rather than by a post-filter -- a query for one project
    physically cannot surface another project's vectors.
    """
    if not sqlite_vec_available():
        return False

    existing = _vector_table_dim(conn)
    if existing is not None and existing != dim:
        # Dimension changed (model swap): drop and force a full re-embed
        # rather than mixing incompatible vector spaces.
        conn.execute(f"DROP TABLE IF EXISTS {VECTOR_TABLE}")
        conn.execute("UPDATE memory_chunks SET embedded_at = NULL, embed_model = NULL")
        conn.commit()
        existing = None

    if existing is None:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0(
                chunk_id TEXT PRIMARY KEY,
                project_key TEXT partition key,
                embedding float[{dim}] distance_metric=cosine
            )
            """
        )
        conn.commit()
    return True


def serialize_vector(values: list[float]) -> bytes:
    import struct

    return struct.pack(f"{len(values)}f", *values)
