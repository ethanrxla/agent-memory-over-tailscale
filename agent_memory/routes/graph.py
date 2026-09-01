"""3D vector-tree graph: JSON data endpoint and the interactive page."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse

from ..context import AppContext
from ..graph import build_graph


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/v1/graph")
    def graph_data(
        project_key: str | None = Query(default=None),
        scope: str = Query(default="global"),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        conn = ctx.open()
        try:
            return build_graph(conn, ctx.config, project_key=project_key, scope=scope)
        finally:
            conn.close()

    browser_auth = ctx.require_browser_key_dep()

    @app.get("/graph", response_class=HTMLResponse)
    def graph_page(
        project_key: str | None = Query(default=None),
        scope: str = Query(default="global"),
        _: None = Depends(browser_auth),
    ) -> HTMLResponse:
        template = (resources.files("agent_memory") / "templates" / "graph.html").read_text("utf-8")
        params = {"scope": scope}
        if project_key:
            params["project_key"] = project_key
        query = "&".join(f"{k}={v}" for k, v in params.items())
        # Hub page fetches live; GRAPH_DATA stays null.
        html = template.replace("__GRAPH_DATA__", "null").replace(
            "__DATA_URL__", json.dumps(f"/v1/graph?{query}")
        )
        return HTMLResponse(html)
