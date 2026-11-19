"""Dashboard freshness, sidebar scrolling and project mute/solo.

The owner saw agents read sessions he could not see in the dashboard: pages
fetched once and never refreshed, "last updated" tracked only indexed chunks,
and nothing forbade caching. The mute/solo state logic is plain JS, tested
under node when it is available.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _ingest(client: TestClient, sid: str, project: str, day: str) -> None:
    r = client.post("/v1/sessions/events", json={
        "session": {"session_id": sid, "project_key": project, "tool": "codex"},
        "events": [{"seq": 0, "ts": f"{day}T09:00:00Z", "content": f"work on {project}"}],
    })
    assert r.status_code == 200


def test_dashboard_is_never_cached_and_ships_live_scripts(client: TestClient) -> None:
    _ingest(client, "s1", "github.com/me/alpha", "2026-11-18")
    r = client.get("/")
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    html = r.text
    assert "/*PROJECT_FILTER_JS*/" not in html and "ProjectFilter" in html and "attachProjectMenu" in html
    for live in ("live-stats", "live-sessions", "live-projects", "refresh-btn", "auto-btn"):
        assert f'id="{live}"' in html
    assert "data-project='github.com/me/alpha'" in html
    assert "no-store" in client.get("/dashboard/data").headers["cache-control"]


def test_last_updated_tracks_ingest_not_just_indexing(client: TestClient) -> None:
    _ingest(client, "s1", "github.com/me/alpha", "2026-11-18")
    data = client.get("/dashboard/data").json()
    # No worker ran, so nothing is indexed; ingest alone must move the clock.
    assert data["stats"]["chunks"] == 0
    assert data["stats"]["last_updated"].startswith("2026-11-18")


def test_projects_listed_by_last_activity_beyond_ten(client: TestClient) -> None:
    for i in range(14):
        _ingest(client, f"s{i}", f"github.com/me/p{i:02d}", f"2026-10-{i + 1:02d}")
    projects = client.get("/dashboard/data").json()["projects"]
    assert len(projects) == 14
    assert projects[0]["project_key"] == "github.com/me/p13"
    assert projects[-1]["project_key"] == "github.com/me/p00"


def test_project_keys_are_attribute_escaped(client: TestClient) -> None:
    _ingest(client, "s1", "path:C:\\it's <odd>", "2026-11-18")
    html = client.get("/").text
    assert "data-project='path:C:\\it&#x27;s &lt;odd&gt;'" in html


def test_graph_page_and_data(client: TestClient) -> None:
    _ingest(client, "a", "github.com/me/zeta", "2026-11-18")
    _ingest(client, "b", "github.com/me/alpha", "2026-01-01")
    page = client.get("/graph")
    assert "no-store" in page.headers["cache-control"]
    assert "__PROJECT_FILTER_JS__" not in page.text and "__CONTEXT_MENU_JS__" not in page.text
    assert "overflow-y:auto" in page.text and "min-height:0" in page.text
    data = client.get("/v1/graph")
    assert "no-store" in data.headers["cache-control"]
    d = data.json()
    assert d["projects"] == ["github.com/me/alpha", "github.com/me/zeta"]  # palette order stable
    assert d["project_meta"]["github.com/me/zeta"]["last_active"].startswith("2026-11-18")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_project_filter_js_state_helpers() -> None:
    script = ROOT / "tests" / "js" / "project_filter.test.js"
    r = subprocess.run([str(NODE), str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok" in r.stdout


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_inline_scripts_parse(client: TestClient, tmp_path: Path) -> None:
    """Every inline <script> on both pages is valid JS (node --check)."""
    _ingest(client, "a", "github.com/me/zeta", "2026-11-18")
    pages = {"dash": client.get("/").text, "graph": client.get("/graph").text}
    count = 0
    for name, html in pages.items():
        for i, body in enumerate(re.findall(r"<script>(.*?)</script>", html, flags=re.S)):
            path = tmp_path / f"{name}-{i}.js"
            path.write_text(body, encoding="utf-8")
            r = subprocess.run([str(NODE), "--check", str(path)], capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, f"{name} script {i}: {r.stderr}"
            count += 1
    assert count >= 5


def test_static_scripts_have_no_dollar_sign() -> None:
    """They are inlined into a string.Template page; keep them substitution-safe."""
    for name in ("project_filter.js", "context_menu.js", "dashboard_live.js"):
        assert "$" not in (ROOT / "agent_memory" / "static" / name).read_text("utf-8"), name
