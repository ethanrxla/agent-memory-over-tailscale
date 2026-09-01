"""Build a 3D 'vector tree' of projects, sessions, and summaries.

Nodes are positioned by the semantics of their embeddings when available
(nearby in the scene == similar in meaning), otherwise by a deterministic
structural layout. Either way the tree edges (project -> session -> summary)
are preserved, and colour encodes project -- so the relevance isolation the
store enforces is visible at a glance.

PCA is done with dual (Gram-matrix) power iteration in pure Python: the number
of nodes N is far smaller than the 2048-dim vectors, so eigen-decomposing the
N x N Gram matrix is much cheaper than the covariance and needs no numpy.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import struct
from typing import Any

from .config import Config
from .db import VECTOR_TABLE, sqlite_vec_available
from .store import parse_ts, resolve_scope


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _gram_pca_3d(vectors: list[list[float]]) -> list[tuple[float, float, float]]:
    """Project vectors to 3D via dual PCA (eigenvectors of the N x N Gram matrix).

    Returns coordinates centred at the origin. Deterministic (seeded).
    """
    n = len(vectors)
    if n == 0:
        return []
    if n == 1:
        return [(0.0, 0.0, 0.0)]

    dim = len(vectors[0])
    mean = [sum(v[j] for v in vectors) / n for j in range(dim)]
    centered = [[v[j] - mean[j] for j in range(dim)] for v in vectors]

    # Gram matrix G = Xc Xc^T  (n x n).
    gram = [[0.0] * n for _ in range(n)]
    for i in range(n):
        gi = gram[i]
        xi = centered[i]
        gi[i] = sum(a * a for a in xi)
        for k in range(i + 1, n):
            dot = sum(xi[j] * centered[k][j] for j in range(dim))
            gi[k] = dot
            gram[k][i] = dot

    rng = random.Random(1234)
    eigvecs: list[list[float]] = []
    coords_axes: list[list[float]] = []

    for axis in range(3):
        vec = [rng.gauss(0, 1) for _ in range(n)]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        vec = [x / norm for x in vec]
        eigval = 0.0
        for _ in range(80):
            nxt = [sum(gram[i][k] * vec[k] for k in range(n)) for i in range(n)]
            # Deflate previously found eigenvectors to keep them orthogonal.
            for prev in eigvecs:
                proj = sum(nxt[i] * prev[i] for i in range(n))
                for i in range(n):
                    nxt[i] -= proj * prev[i]
            eigval = math.sqrt(sum(x * x for x in nxt))
            if eigval < 1e-12:
                break
            nxt = [x / eigval for x in nxt]
            if sum(a * b for a, b in zip(nxt, vec)) > 0.999999:
                vec = nxt
                break
            vec = nxt
        eigvecs.append(vec)
        # Principal coordinate on this axis = eigvec * sqrt(eigenvalue).
        scale = math.sqrt(max(eigval, 0.0))
        coords_axes.append([component * scale for component in vec])

    return [(coords_axes[0][i], coords_axes[1][i], coords_axes[2][i]) for i in range(n)]


def _fibonacci_sphere(count: int, radius: float = 1.0) -> list[tuple[float, float, float]]:
    """Evenly spread points on a sphere -- the structural fallback layout."""
    if count <= 0:
        return []
    if count == 1:
        return [(0.0, 0.0, 0.0)]
    points = []
    phi = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(count):
        y = 1 - (i / (count - 1)) * 2
        r = math.sqrt(max(0.0, 1 - y * y))
        theta = phi * i
        points.append((math.cos(theta) * r * radius, y * radius, math.sin(theta) * r * radius))
    return points


def _normalize(coords: list[tuple[float, float, float]], span: float = 100.0) -> list[tuple[float, float, float]]:
    if not coords:
        return []
    max_abs = max((max(abs(x), abs(y), abs(z)) for x, y, z in coords), default=1.0) or 1.0
    factor = span / max_abs
    return [(x * factor, y * factor, z * factor) for x, y, z in coords]


def build_graph(
    conn: sqlite3.Connection,
    config: Config,
    *,
    project_key: str | None = None,
    scope: str = "global",
    max_summary_nodes: int = 400,
) -> dict[str, Any]:
    """Assemble the node/edge graph with 3D coordinates.

    Leaf nodes are session summaries (one per session, the 'final' tier when
    present). Their positions drive the layout: from embeddings if the summary
    chunks are embedded, otherwise structurally. Sessions sit at the centroid
    of their summaries; projects at the centroid of their sessions.
    """
    project_keys = resolve_scope(conn, project_key, scope) if project_key else None

    where = ""
    params: list[Any] = []
    if project_keys is not None:
        placeholders = ",".join("?" for _ in project_keys)
        where = f"WHERE s.project_key IN ({placeholders})"
        params = list(project_keys)

    sessions = conn.execute(
        f"""
        SELECT s.session_id, s.project_key, s.tool, s.ai_title, s.status,
               s.event_count, s.token_estimate, s.started_at, s.last_event_at,
               s.last_prompt, s.cwd, s.git_branch, s.device_name,
               (SELECT summary FROM session_summaries
                WHERE session_id = s.session_id
                ORDER BY CASE tier WHEN 'final' THEN 0 ELSE 1 END LIMIT 1) AS summary,
               (SELECT decisions_json FROM session_summaries
                WHERE session_id = s.session_id
                ORDER BY CASE tier WHEN 'final' THEN 0 ELSE 1 END LIMIT 1) AS decisions_json,
               (SELECT open_threads_json FROM session_summaries
                WHERE session_id = s.session_id
                ORDER BY CASE tier WHEN 'final' THEN 0 ELSE 1 END LIMIT 1) AS threads_json
        FROM sessions s
        {where}
        {'AND' if where else 'WHERE'} s.is_sidechain = 0
        ORDER BY s.last_event_at DESC
        LIMIT ?
        """,
        [*params, max_summary_nodes],
    ).fetchall()

    # Pull the summary chunk id + vector for each session (if embedded).
    vectors: dict[str, list[float]] = {}
    have_vectors = sqlite_vec_available()
    for row in sessions:
        sid = row["session_id"]
        chunk = conn.execute(
            """
            SELECT chunk_id FROM memory_chunks
            WHERE source_type = 'session_summary' AND session_id = ?
            ORDER BY CASE WHEN source_ref LIKE '%:final' THEN 0 ELSE 1 END LIMIT 1
            """,
            (sid,),
        ).fetchone()
        if chunk and have_vectors:
            try:
                vrow = conn.execute(
                    f"SELECT embedding FROM {VECTOR_TABLE} WHERE chunk_id = ?", (chunk["chunk_id"],)
                ).fetchone()
            except sqlite3.OperationalError:
                vrow = None
            if vrow and vrow["embedding"]:
                vectors[sid] = _unpack(vrow["embedding"])

    session_ids = [row["session_id"] for row in sessions]
    embedded_ids = [sid for sid in session_ids if sid in vectors]

    positions: dict[str, tuple[float, float, float]] = {}
    used_embeddings = len(embedded_ids) >= 3
    if used_embeddings:
        coords = _normalize(_gram_pca_3d([vectors[sid] for sid in embedded_ids]))
        for sid, coord in zip(embedded_ids, coords):
            positions[sid] = coord
        # Place any unembedded sessions on a small outer shell so they are visible.
        leftovers = [sid for sid in session_ids if sid not in positions]
        for sid, coord in zip(leftovers, _normalize(_fibonacci_sphere(len(leftovers)), span=120.0)):
            positions[sid] = coord
    else:
        for sid, coord in zip(session_ids, _normalize(_fibonacci_sphere(len(session_ids)), span=100.0)):
            positions[sid] = coord

    # Group sessions by project for centroids and colour assignment.
    by_project: dict[str, list[sqlite3.Row]] = {}
    for row in sessions:
        by_project.setdefault(row["project_key"], []).append(row)

    project_palette = sorted(by_project.keys())
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    for pkey, rows in by_project.items():
        pts = [positions[r["session_id"]] for r in rows]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        cz = sum(p[2] for p in pts) / len(pts)
        display = conn.execute(
            "SELECT display_name FROM projects WHERE project_key = ?", (pkey,)
        ).fetchone()
        pid = f"project:{pkey}"
        # A one-session project has the same centroid as its only session. Move
        # every project root toward the origin so roots and leaves remain
        # visibly separate and independently clickable.
        root_scale = 0.72
        px, py, pz = cx * root_scale, cy * root_scale, cz * root_scale
        nodes.append({
            "id": pid,
            "type": "project",
            "label": display["display_name"] if display else pkey.rsplit("/", 1)[-1],
            "project_key": pkey,
            "color_index": project_palette.index(pkey),
            "x": px, "y": py, "z": pz,
            "size": 6.0,
            "meta": {"sessions": len(rows)},
        })
        for r in rows:
            sid = r["session_id"]
            x, y, z = positions[sid]
            nodes.append({
                "id": f"session:{sid}",
                "type": "session",
                "label": r["ai_title"] or sid[:8],
                "project_key": pkey,
                "color_index": project_palette.index(pkey),
                "x": x, "y": y, "z": z,
                "size": 2.0 + min(4.0, (r["event_count"] or 0) / 200.0),
                "meta": {
                    "session_id": sid,
                    "tool": r["tool"],
                    "status": r["status"],
                    "events": r["event_count"],
                    "tokens": r["token_estimate"],
                    "started_at": r["started_at"],
                    "branch": r["git_branch"],
                    "device": r["device_name"],
                    "cwd": r["cwd"],
                    "last_prompt": r["last_prompt"],
                    "last_event_at": r["last_event_at"],
                    "summary": r["summary"],
                    "summary_decisions": _json_list(r["decisions_json"]),
                    "summary_threads": _json_list(r["threads_json"]),
                },
            })
            edges.append({"source": pid, "target": f"session:{sid}"})

    return {
        "nodes": nodes,
        "edges": edges,
        "projects": project_palette,
        "layout": "semantic" if used_embeddings else "structural",
        "embedded_sessions": len(embedded_ids),
        "total_sessions": len(session_ids),
        "scope": {"project_key": project_key, "scope": scope},
    }
