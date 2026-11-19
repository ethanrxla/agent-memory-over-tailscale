"""Idempotent re-ingest, hub-to-hub sync, and honest session activity times.

Two hubs (pop-os and the Windows side of the same PC) each keep a database.
Sessions uploaded to one while the other was down are pushed across with
``memory.py sync``; that is only safe because ingest is idempotent.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from agent_memory.hubs import load_resolver
from tests.conftest import register

memory: Any = load_resolver()


def _session(sid: str, **kw) -> dict:
    return {"session_id": sid, "project_key": kw.pop("project_key", "github.com/me/proj"),
            "tool": "claude-code", "device_name": "RXLA_PC_1", **kw}


def _events(n: int, day: str = "2026-03-01", start: int = 0) -> list[dict]:
    return [{"seq": i, "ts": f"{day}T10:{i:02d}:00Z", "role": "user", "kind": "prompt",
             "content": f"step {i} of the widget work"} for i in range(start, start + n)]


def test_reingest_is_idempotent(client: TestClient) -> None:
    body = {"session": _session("s-1"), "events": _events(5)}
    first = client.post("/v1/sessions/events", json=body).json()
    again = client.post("/v1/sessions/events", json=body).json()
    assert (first["inserted"], first["skipped"]) == (5, 0)
    assert (again["inserted"], again["skipped"]) == (0, 5)
    s = client.get("/v1/sessions/s-1").json()["session"]
    assert s["event_count"] == 5


def test_backfilled_session_keeps_its_real_activity_time(client: TestClient) -> None:
    # A months-old transcript uploaded today...
    client.post("/v1/sessions/events", json={"session": _session("old"), "events": _events(3, "2026-01-05")})
    # ...must not outrank work that actually happened recently.
    client.post("/v1/sessions/events", json={"session": _session("recent"), "events": _events(2, "2026-11-18")})
    client.post("/v1/sessions/events", json={"session": _session("old"), "events": _events(1, "2026-01-05", start=3)})

    rows = client.get("/v1/sessions", params={"project_key": "github.com/me/proj"}).json()["sessions"]
    assert [r["session_id"] for r in rows] == ["recent", "old"]
    old = next(r for r in rows if r["session_id"] == "old")
    assert old["last_event_at"].startswith("2026-01-05T10:03")

    from agent_memory.db import create_connection

    conn = create_connection(client.app.state.db_path)
    try:
        ingest = conn.execute("SELECT last_ingest_at FROM sessions WHERE session_id='old'").fetchone()[0]
    finally:
        conn.close()
    assert ingest and not ingest.startswith("2026-01-05")  # upload time kept separately


def _router(hubs: dict[str, TestClient]):
    """memory._http stand-in that dispatches http://<name>/... to TestClients."""
    def http(method, url, payload=None, key="", timeout=30):
        parts = urlsplit(url)
        tc = hubs[f"{parts.scheme}://{parts.netloc}"]
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        r = tc.request(method, path, json=payload, headers={"X-Shared-Key": key} if key else {})
        assert r.status_code == 200, r.text
        return r.json()
    return http


def _hub(tmp_path, name: str) -> TestClient:
    from agent_memory.app import create_app

    return TestClient(create_app(db_path=str(tmp_path / f"{name}.db"), shared_key="", worker_enabled=False))


def test_sync_pushes_sessions_and_decisions_and_is_idempotent(tmp_path) -> None:
    windows, popos = _hub(tmp_path, "windows"), _hub(tmp_path, "popos")
    windows.post("/v1/sessions/events", json={"session": _session("w-1", ai_title="Fix dashboard"),
                                               "events": _events(1200)})
    windows.post("/v1/sessions/events", json={"session": _session("w-2", project_key="path:C:\\x"),
                                               "events": _events(3, "2026-11-17")})
    # pop-os already had part of w-1 (e.g. delivered before the reboot).
    popos.post("/v1/sessions/events", json={"session": _session("w-1"), "events": _events(10)})
    register(windows, "codex-win", device_name="RXLA_PC_1")
    windows.post("/v1/decisions", json={"agent_id": "codex-win", "project_key": "github.com/me/proj",
                                        "title": "Use sqlite-vec", "content": "One file on the tailnet."})

    http = _router({"http://windows": windows, "http://popos": popos})
    logs: list[str] = []
    first = memory.sync_hubs("http://windows", "http://popos", http=http, log=logs.append)
    assert first["sessions"] == 2 and first["events_sent"] == 1203
    assert first["inserted"] == 1193 and first["skipped"] == 10 and first["entries"] == 1

    second = memory.sync_hubs("http://windows", "http://popos", http=http, log=logs.append)
    assert second["inserted"] == 0 and second["skipped"] == 1203

    s = popos.get("/v1/sessions/w-1").json()["session"]
    assert s["event_count"] == 1200 and s["title"] == "Fix dashboard"
    assert s["last_event_at"] == windows.get("/v1/sessions/w-1").json()["session"]["last_event_at"]
    hits = popos.post("/v1/search", json={"query": "sqlite-vec", "scope": "global"}).json()["results"]
    assert any("sqlite-vec" in h["title"] for h in hits)
    from agent_memory.db import create_connection

    conn = create_connection(popos.app.state.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1  # not duplicated
    finally:
        conn.close()
    assert "already there" in logs[-1]


def test_sync_since_and_dry_run(tmp_path) -> None:
    a, b = _hub(tmp_path, "a"), _hub(tmp_path, "b")
    a.post("/v1/sessions/events", json={"session": _session("old"), "events": _events(2, "2026-01-01")})
    a.post("/v1/sessions/events", json={"session": _session("new"), "events": _events(2, "2026-11-18")})
    http = _router({"http://a": a, "http://b": b})
    dry = memory.sync_hubs("http://a", "http://b", since="2026-11-01", dry_run=True, http=http, log=lambda m: None)
    assert dry["sessions"] == 1 and dry["inserted"] == 0
    assert b.get("/v1/sessions").json()["sessions"] == []


def test_export_requires_key(keyed_client: TestClient) -> None:
    assert keyed_client.get("/v1/export/sessions").status_code == 401
    assert keyed_client.get("/v1/export/sessions", headers={"X-Shared-Key": "test-key"}).status_code == 200


def test_export_events_pages_in_seq_order(client: TestClient) -> None:
    client.post("/v1/sessions/events", json={"session": _session("p"), "events": _events(5)})
    page = client.get("/v1/export/sessions/p/events", params={"after_seq": 1, "limit": 2}).json()
    assert [e["seq"] for e in page["events"]] == [2, 3]
    assert client.get("/v1/export/sessions/missing/events").status_code == 404
