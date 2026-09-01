"""Hybrid retrieval: BM25 + vector KNN fused with RRF, scoped and floored.

Three layers of relevance isolation, applied in order:

1. Hard scope -- ``project_key``. A partition, not a ranking signal. Vector
   search uses it as a vec0 partition key, so another project's vectors are
   physically unreachable; keyword search applies it as a SQL predicate.
2. Soft rank -- reciprocal rank fusion of BM25 and cosine, times an
   exponential recency decay.
3. Relevance floor -- an absolute ``min_score`` gate. Below it, results are
   dropped and the caller is told there is no relevant prior context, rather
   than being handed marginal matches that send an agent off-track.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .db import VECTOR_TABLE, serialize_vector, sqlite_vec_available
from .store import parse_ts, resolve_scope
from .text import build_fts_query, query_terms, term_coverage

# bm25 weights, one per FTS column: chunk_id (UNINDEXED), title, tags, content.
# The count must equal the column count or the weights silently shift onto the
# wrong columns -- the bug this replaces passed 7 weights for 8 columns.
BM25_WEIGHTS = (0.0, 3.0, 2.0, 1.0)

CANDIDATE_MULTIPLIER = 12
MIN_CANDIDATES = 60


def _visibility_clause(requester_agent_id: str | None) -> tuple[str, list[Any]]:
    if requester_agent_id:
        return (
            "(c.visibility IN ('shared', 'broadcast') OR c.agent_id = ? OR c.recipient_id = ?)",
            [requester_agent_id, requester_agent_id],
        )
    return ("c.visibility IN ('shared', 'broadcast')", [])


def _filters(req: Any, project_keys: list[str] | None) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if project_keys is not None:
        placeholders = ",".join("?" for _ in project_keys)
        clauses.append(f"c.project_key IN ({placeholders})")
        params.extend(project_keys)
    if getattr(req, "namespace", None):
        clauses.append("c.namespace = ?")
        params.append(req.namespace)
    if getattr(req, "agent_id", None):
        clauses.append("c.agent_id = ?")
        params.append(req.agent_id)
    if getattr(req, "kind", None):
        clauses.append("c.kind = ?")
        params.append(req.kind)
    if getattr(req, "session_id", None):
        clauses.append("c.session_id = ?")
        params.append(req.session_id)
    if getattr(req, "tag", None):
        clauses.append("(' ' || c.tags || ' ') LIKE ?")
        params.append(f"% {req.tag.strip().lower()} %")
    if getattr(req, "source_types", None):
        types = list(req.source_types)
        placeholders = ",".join("?" for _ in types)
        clauses.append(f"c.source_type IN ({placeholders})")
        params.extend(types)
    if not getattr(req, "include_sidechain", False):
        clauses.append("c.is_sidechain = 0")

    visibility_sql, visibility_params = _visibility_clause(getattr(req, "requester_agent_id", None))
    clauses.append(visibility_sql)
    params.extend(visibility_params)
    return clauses, params


def _recency_factor(ts: str | None, half_life_days: float) -> float:
    """Exponential decay so current context outranks equally-similar stale context."""
    parsed = parse_ts(ts)
    if parsed is None or half_life_days <= 0:
        return 1.0
    age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def _fts_candidates(
    conn: sqlite3.Connection, req: Any, project_keys: list[str] | None, limit: int
) -> list[sqlite3.Row]:
    match_query = build_fts_query(req.query)
    if not match_query:
        return []
    clauses, params = _filters(req, project_keys)
    where = " AND ".join(["memory_chunks_fts MATCH ?"] + clauses)
    # Bind order follows the SQL text: bm25 weights (SELECT), MATCH expression
    # (WHERE), filter params, then LIMIT.
    try:
        return conn.execute(
            f"""
            SELECT c.*, bm25(memory_chunks_fts, ?, ?, ?, ?) AS bm25_score
            FROM memory_chunks_fts
            JOIN memory_chunks c ON c.chunk_id = memory_chunks_fts.chunk_id
            WHERE {where}
            ORDER BY bm25_score ASC
            LIMIT ?
            """,
            [*BM25_WEIGHTS, match_query, *params, limit],
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _vector_candidates(
    conn: sqlite3.Connection,
    req: Any,
    project_keys: list[str] | None,
    limit: int,
    vector: list[float],
) -> list[tuple[sqlite3.Row, float]]:
    if not sqlite_vec_available():
        return []
    blob = serialize_vector(vector)
    rows: list[tuple[str, float]] = []
    try:
        if project_keys:
            for key in project_keys:
                found = conn.execute(
                    f"""
                    SELECT chunk_id, distance FROM {VECTOR_TABLE}
                    WHERE embedding MATCH ? AND k = ? AND project_key = ?
                    """,
                    (blob, limit, key),
                ).fetchall()
                rows.extend((row["chunk_id"], row["distance"]) for row in found)
        else:
            found = conn.execute(
                f"SELECT chunk_id, distance FROM {VECTOR_TABLE} WHERE embedding MATCH ? AND k = ?",
                (blob, limit),
            ).fetchall()
            rows.extend((row["chunk_id"], row["distance"]) for row in found)
    except sqlite3.OperationalError:
        return []

    if not rows:
        return []
    rows.sort(key=lambda item: item[1])
    rows = rows[:limit]

    ids = [chunk_id for chunk_id, _ in rows]
    placeholders = ",".join("?" for _ in ids)
    clauses, params = _filters(req, project_keys)
    where = " AND ".join([f"c.chunk_id IN ({placeholders})"] + clauses)
    found_rows = conn.execute(
        f"SELECT c.* FROM memory_chunks c WHERE {where}", [*ids, *params]
    ).fetchall()
    by_id = {row["chunk_id"]: row for row in found_rows}
    distances = dict(rows)
    return [(by_id[i], distances[i]) for i in ids if i in by_id]


def search(
    conn: sqlite3.Connection,
    config: Config,
    req: Any,
    *,
    query_vector: list[float] | None = None,
) -> dict[str, Any]:
    project_keys = resolve_scope(conn, getattr(req, "project_key", None), getattr(req, "scope", "project"))
    limit = int(getattr(req, "limit", 8))
    candidate_limit = max(MIN_CANDIDATES, limit * CANDIDATE_MULTIPLIER)

    fts_rows = _fts_candidates(conn, req, project_keys, candidate_limit)
    vec_rows = (
        _vector_candidates(conn, req, project_keys, candidate_limit, query_vector)
        if query_vector
        else []
    )

    k = max(1, config.rrf_k)
    fused: dict[str, dict[str, Any]] = {}

    for rank, row in enumerate(fts_rows):
        fused[row["chunk_id"]] = {
            "row": row,
            "rrf": 1.0 / (k + rank + 1),
            "bm25": row["bm25_score"],
            "similarity": None,
        }

    for rank, (row, distance) in enumerate(vec_rows):
        # sqlite-vec cosine distance is 1 - cosine_similarity.
        similarity = max(0.0, min(1.0, 1.0 - float(distance)))
        entry = fused.get(row["chunk_id"])
        if entry is None:
            fused[row["chunk_id"]] = {
                "row": row,
                "rrf": 1.0 / (k + rank + 1),
                "bm25": None,
                "similarity": similarity,
            }
        else:
            entry["rrf"] += 1.0 / (k + rank + 1)
            entry["similarity"] = similarity

    terms = query_terms(req.query)
    max_rrf = (1.0 / (k + 1)) * (2 if vec_rows else 1)
    min_score = getattr(req, "min_score", None)
    if min_score is None:
        min_score = config.default_min_score

    scored: list[dict[str, Any]] = []
    for item in fused.values():
        row = item["row"]
        haystack = f"{row['title']} {row['tags']} {row['content']}"
        coverage = term_coverage(terms, haystack)
        similarity = item["similarity"]
        # Absolute relevance: strong on either signal is enough to qualify.
        relevance = max(coverage, similarity if similarity is not None else 0.0)
        recency = _recency_factor(row["ts"], config.recency_half_life_days)
        rank_score = (item["rrf"] / max_rrf if max_rrf else 0.0) * (0.4 + 0.6 * recency)

        if relevance < min_score:
            continue
        scored.append(
            {
                "row": row,
                "rank_score": rank_score,
                "relevance": relevance,
                "coverage": coverage,
                "similarity": similarity,
                "recency": recency,
            }
        )

    scored.sort(key=lambda item: (item["rank_score"], item["relevance"]), reverse=True)

    # Entry-level dedup: several chunks of one document must not consume the
    # caller's limit. Keep the best chunk and note how many others matched.
    results: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for item in scored:
        row = item["row"]
        key = f"{row['source_type']}:{row['source_ref']}"
        if key in seen:
            seen[key]["additional_chunks"] += 1
            continue
        payload = {
            "chunk_id": row["chunk_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "entry_id": row["source_ref"] if row["source_type"] == "entry" else None,
            "session_id": row["session_id"],
            "project_key": row["project_key"],
            "namespace": row["namespace"],
            "agent_id": row["agent_id"],
            "kind": row["kind"],
            "title": row["title"],
            "content": row["content"],
            "tags": [t for t in (row["tags"] or "").split() if t],
            "visibility": row["visibility"],
            "recipient_id": row["recipient_id"],
            "updated_at": row["ts"],
            "chunk_index": row["chunk_index"],
            "score": round(item["rank_score"], 6),
            "relevance": round(item["relevance"], 4),
            "term_coverage": round(item["coverage"], 4),
            "similarity": round(item["similarity"], 4) if item["similarity"] is not None else None,
            "additional_chunks": 0,
        }
        seen[key] = payload
        results.append(payload)
        if len(results) >= limit:
            break

    return {
        "results": results,
        "scope": {
            "project_key": getattr(req, "project_key", None),
            "scope": getattr(req, "scope", "project"),
            "projects_searched": project_keys,
            "min_score": min_score,
        },
        "retrieval": {
            "keyword_candidates": len(fts_rows),
            "vector_candidates": len(vec_rows),
            "vector_search_used": bool(vec_rows),
            "fused_candidates": len(fused),
        },
        "message": (
            None
            if results
            else "No relevant prior context found for this query in scope. "
                 "Proceed without prior context rather than guessing."
        ),
    }
