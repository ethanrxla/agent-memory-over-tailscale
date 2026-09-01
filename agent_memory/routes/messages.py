"""Agent-to-agent message inbox."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, Query

from ..context import AppContext


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/v1/messages/inbox/{agent_id}")
    def inbox(
        agent_id: str,
        namespace: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        clauses = ["kind = 'message'", "(recipient_id = ? OR visibility = 'broadcast')"]
        params: list[Any] = [agent_id]
        if namespace:
            clauses.append("namespace = ?")
            params.append(namespace)
        params.append(limit)

        conn = ctx.open()
        try:
            rows = conn.execute(
                f"""
                SELECT entry_id, agent_id, namespace, source_id, title, content, tags_json,
                       recipient_id, visibility, source_uri, metadata_json, created_at, updated_at
                FROM entries
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        finally:
            conn.close()

        return {
            "messages": [
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
                for row in rows
            ]
        }
