"""3D vector-tree graph: JSON data endpoint and the interactive page."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import HTMLResponse

from ..context import AppContext
from ..graph import build_graph

# Browsers and proxies must never serve a stale copy of live memory data:
# a cached /v1/graph is exactly how new sessions "don't show up".
NO_STORE = {"Cache-Control": "no-store, max-age=0"}


def static_js(name: str) -> str:
    """Contents of agent_memory/static/<name>, for inlining into a page."""
    return (resources.files("agent_memory") / "static" / name).read_text("utf-8")


def inline_scripts(html: str) -> str:
    return html.replace("__PROJECT_FILTER_JS__", static_js("project_filter.js")).replace(
        "__CONTEXT_MENU_JS__", static_js("context_menu.js")
    )


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/v1/graph")
    def graph_data(
        response: Response,
        project_key: str | None = Query(default=None),
        scope: str = Query(default="global"),
        _: None = Depends(ctx.require_key_dep()),
    ) -> dict[str, Any]:
        response.headers.update(NO_STORE)
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
        query = urlencode(params)
        # Hub page fetches live; GRAPH_DATA stays null.
        html = inline_scripts(template).replace("__GRAPH_DATA__", "null").replace(
            "__DATA_URL__", json.dumps(f"/v1/graph?{query}")
        )
        return HTMLResponse(html, headers=NO_STORE)
