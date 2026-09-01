"""Session ingestion, idempotency, summaries, and the project brief."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _session(session_id: str = "s1", project_key: str = "path:/repo/a", **kw):
    base = {
        "session_id": session_id, "project_key": project_key, "tool": "claude-code",
        "cwd": "/repo/a", "git_branch": "main", "ai_title": "Build the thing",
    }
    base.update(kw)
    return base


def _events(n: int, start: int = 0):
    events = []
    for i in range(start, start + n):
        events.append({
            "seq": i, "role": "user" if i % 2 == 0 else "assistant",
            "kind": "prompt" if i % 2 == 0 else "reply",
            "content": f"event number {i} about the widget subsystem",
        })
    return events


def test_events_ingest_and_count(client: TestClient) -> None:
    r = client.post("/v1/sessions/events", json={"session": _session(), "events": _events(4)})
    assert r.status_code == 200
    assert r.json()["inserted"] == 4

    detail = client.get("/v1/sessions/s1").json()
    assert detail["session"]["event_count"] == 4
    assert detail["session"]["project_key"] == "path:/repo/a"


def test_event_ingest_is_idempotent(client: TestClient) -> None:
    """Replaying a spool must not duplicate events (dedup on session_id, seq)."""
    payload = {"session": _session(), "events": _events(5)}
    first = client.post("/v1/sessions/events", json=payload).json()
    second = client.post("/v1/sessions/events", json=payload).json()
    assert first["inserted"] == 5
    assert second["inserted"] == 0
    assert second["skipped"] == 5
    assert client.get("/v1/sessions/s1").json()["session"]["event_count"] == 5


def test_partial_replay_only_inserts_new_events(client: TestClient) -> None:
    client.post("/v1/sessions/events", json={"session": _session(), "events": _events(3)})
    # Overlapping batch: seqs 2,3,4 -- only 3 and 4 are new.
    result = client.post("/v1/sessions/events", json={"session": _session(), "events": _events(3, start=2)}).json()
    assert result["inserted"] == 2
    assert client.get("/v1/sessions/s1").json()["session"]["event_count"] == 5


def test_sessions_are_scoped_by_project(client: TestClient) -> None:
    client.post("/v1/sessions/events", json={"session": _session("a", "path:/repo/a"), "events": _events(2)})
    client.post("/v1/sessions/events", json={"session": _session("b", "path:/repo/b"), "events": _events(2)})

    only_a = client.get("/v1/sessions", params={"project_key": "path:/repo/a"}).json()["sessions"]
    assert [s["session_id"] for s in only_a] == ["a"]


def test_extractive_brief_without_nim(client: TestClient) -> None:
    """The brief works with no model: summaries fall back to structure."""
    from agent_memory.nim import NimClient
    from agent_memory.summarize import generate_summary

    client.post("/v1/sessions/events", json={
        "session": _session(),
        "events": [
            {"seq": 0, "role": "user", "kind": "prompt", "content": "Add retry logic to the uploader"},
            {"seq": 1, "role": "assistant", "kind": "tool_use", "content": "Edit uploader.py",
             "files": ["/repo/a/uploader.py"]},
            {"seq": 2, "role": "assistant", "kind": "tool_use", "content": "$ pytest",
             "commands": ["pytest -q"]},
        ],
    })

    ctx = client.app.state.ctx
    conn = ctx.open()
    try:
        result = generate_summary(conn, ctx.config, NimClient(ctx.config), "s1", tier="final")
        conn.commit()
    finally:
        conn.close()

    assert result["model"] == "extractive"
    assert "uploader.py" in result["summary"]

    brief = client.get("/v1/projects/path:/repo/a/brief").json()
    assert brief["session_count"] == 1
    assert brief["estimated_tokens"] <= brief["token_budget"]
    assert "uploader.py" in brief["sessions"][0]["summary"]


def test_brief_stays_within_token_budget(client: TestClient) -> None:
    from agent_memory.nim import NimClient
    from agent_memory.summarize import generate_summary

    ctx = client.app.state.ctx
    for i in range(8):
        sid = f"sess-{i}"
        client.post("/v1/sessions/events", json={
            "session": _session(sid, ai_title=f"Session {i} " + "detail " * 40),
            "events": _events(3),
        })
        conn = ctx.open()
        try:
            generate_summary(conn, ctx.config, NimClient(ctx.config), sid, tier="final")
            conn.commit()
        finally:
            conn.close()

    brief = client.get("/v1/projects/path:/repo/a/brief", params={"token_budget": 500}).json()
    assert brief["estimated_tokens"] <= 800  # hard ceiling from the plan
    assert brief["session_count"] >= 1


def test_open_threads_endpoint(client: TestClient) -> None:
    ctx = client.app.state.ctx
    client.post("/v1/sessions/events", json={"session": _session(), "events": _events(2)})
    conn = ctx.open()
    try:
        conn.execute(
            """
            INSERT INTO session_summaries (session_id, tier, summary, open_threads_json, generated_at)
            VALUES ('s1', 'final', 'did stuff', '["wire up the dashboard", "add auth"]', datetime('now'))
            """
        )
        conn.commit()
    finally:
        conn.close()

    threads = client.get("/v1/projects/path:/repo/a/threads").json()["open_threads"]
    assert {t["thread"] for t in threads} == {"wire up the dashboard", "add auth"}
