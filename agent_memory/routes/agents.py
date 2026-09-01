"""Agent registration and listing."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, Query

from ..context import AppContext
from ..models import AgentRegistration
from ..store import utc_now


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.post("/v1/agents/register")
    def register_agent(
        payload: AgentRegistration, _: None = Depends(ctx.require_key_dep())
    ) -> dict[str, Any]:
        now = utc_now()
        conn = ctx.open()
        try:
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, display_name, device_name, tailscale_name,
                    capabilities_json, metadata_json, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            rows = conn.execute(
                """
                SELECT agent_id, display_name, device_name, tailscale_name,
                       capabilities_json, metadata_json, last_seen_at
                FROM agents ORDER BY last_seen_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return {
            "agents": [
                {
                    "agent_id": row["agent_id"],
                    "display_name": row["display_name"],
                    "device_name": row["device_name"],
                    "tailscale_name": row["tailscale_name"],
                    "capabilities": json.loads(row["capabilities_json"]),
                    "metadata": json.loads(row["metadata_json"]),
                    "last_seen_at": row["last_seen_at"],
                }
                for row in rows
            ]
        }
