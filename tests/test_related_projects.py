"""A brief with no sessions must name the keys that do hold the history.

project_key is a hard partition, so the same work reached from another machine
or checkout lands under a different key. Reporting "no prior sessions" without
naming those keys is worse than useless -- it reads as "this is new work" and
the agent starts inventing. This lives hub-side so the CLI, the MCP server, and
any older client already in the field all get it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_memory.projects import search_seed
from tests.conftest import register

# The real failure: RedLamb built on Windows, opened from a Mac drive.
MAC = "path:/Volumes/T7 Shield/T7 shield/RedLamb-build"
WSL = "path:/home/theif"
WIN = r"path:C:\Users\theif"


def _seed_session(client: TestClient, project_key: str, session_id: str, text: str) -> None:
    """Ingest a session and summarise it, so it is both briefable and searchable."""
    from agent_memory.nim import NimClient
    from agent_memory.summarize import generate_summary

    session = {
        "session_id": session_id, "project_key": project_key, "tool": "claude-code",
        "cwd": "/tmp", "git_branch": "main", "ai_title": text[:40],
    }
    events = [
        {"seq": i, "role": "user" if i % 2 == 0 else "assistant",
         "kind": "prompt" if i % 2 == 0 else "reply", "content": text}
        for i in range(4)
    ]
    assert client.post(
        "/v1/sessions/events", json={"session": session, "events": events}
    ).status_code == 200

    ctx = client.app.state.ctx
    conn = ctx.open()
    try:
        generate_summary(conn, ctx.config, NimClient(ctx.config), session_id, tier="final")
        conn.commit()
    finally:
        conn.close()


# --- the seed --------------------------------------------------------------

def test_search_seed_strips_scaffolding_from_a_path_key():
    assert search_seed(MAC) == "RedLamb"


def test_search_seed_handles_windows_separators():
    assert search_seed(WIN) == "theif"


def test_search_seed_uses_the_repo_name_of_a_remote_key():
    assert search_seed("github.com/ethanrxla/smartcalendar") == "smartcalendar"


def test_search_seed_keeps_the_name_when_every_token_is_generic():
    assert search_seed("path:/srv/build") == "build"


# --- the brief -------------------------------------------------------------

def test_empty_brief_names_related_projects(client: TestClient) -> None:
    register(client)
    _seed_session(client, WSL, "s-wsl", "RedLamb JUCE plugin DimensionCore orbit rings")
    _seed_session(client, WIN, "s-win", "RedLamb build_redlamb.bat VST3 deploy")

    brief = client.get(f"/v1/projects/{MAC}/brief").json()

    assert brief["sessions"] == []
    keys = {p["project_key"] for p in brief["related_projects"]}
    assert WSL in keys and WIN in keys


def test_hint_is_embedded_in_the_message_for_older_clients(client: TestClient) -> None:
    """Clients already installed on other devices only print `message`."""
    register(client)
    _seed_session(client, WSL, "s-wsl", "RedLamb JUCE plugin DimensionCore orbit rings")

    brief = client.get(f"/v1/projects/{MAC}/brief").json()

    assert "Related projects" in brief["message"]
    assert WSL in brief["message"]


def test_a_brief_with_sessions_has_no_hint(client: TestClient) -> None:
    register(client)
    _seed_session(client, WSL, "s-wsl", "RedLamb JUCE plugin work")

    brief = client.get(f"/v1/projects/{WSL}/brief").json()

    assert brief["message"] is None
    assert brief["related_projects"] == []


def test_project_never_suggests_itself(client: TestClient) -> None:
    register(client)
    _seed_session(client, WSL, "s-wsl", "RedLamb JUCE plugin work")

    brief = client.get(f"/v1/projects/{WSL}/brief").json()
    assert all(p["project_key"] != WSL for p in brief["related_projects"])


def test_unrelated_projects_are_not_suggested(client: TestClient) -> None:
    """A forced weak match is what sends an agent off track."""
    register(client)
    _seed_session(client, "path:/home/x/tax-returns", "s-tax",
                  "quarterly tax spreadsheet reconciliation and invoices")

    brief = client.get(f"/v1/projects/{MAC}/brief").json()

    assert brief["related_projects"] == []
    assert "Related projects" not in (brief["message"] or "")


def test_sibling_paths_are_found_lexically(client: TestClient) -> None:
    """Keyword-only mode (no embeddings in tests) must still catch neighbours."""
    register(client)
    _seed_session(client, "path:/Users/e/scholarcal/smartcalendar", "s-sc", "calendar sync")

    brief = client.get("/v1/projects/path:/Users/e/scholarcal/brief").json()

    keys = {p["project_key"] for p in brief["related_projects"]}
    assert "path:/Users/e/scholarcal/smartcalendar" in keys


def test_empty_projects_are_not_suggested(client: TestClient) -> None:
    register(client)
    client.post("/v1/sessions/upsert", json={
        "session_id": "s-empty", "agent_id": "agent-1", "tool": "claude-code",
        "project_key": "path:/home/theif/RedLamb-empty", "cwd": "/tmp", "status": "idle",
    })

    brief = client.get(f"/v1/projects/{MAC}/brief").json()
    assert all(p["session_count"] > 0 for p in brief["related_projects"])
