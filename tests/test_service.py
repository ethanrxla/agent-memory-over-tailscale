from pathlib import Path

from fastapi.testclient import TestClient

from main import create_app


def make_client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "agent-memory.db"
    app = create_app(db_path=str(db_path), shared_key="test-key")
    return TestClient(app)


def test_publish_and_search_shared_note(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    headers = {"X-Shared-Key": "test-key"}

    register = client.post(
        "/v1/agents/register",
        headers=headers,
        json={
            "agent_id": "codex-laptop",
            "display_name": "Codex Laptop",
            "device_name": "laptop",
            "tailscale_name": "laptop.tailnet.ts.net",
            "capabilities": ["python"],
            "metadata": {"role": "builder"},
        },
    )
    assert register.status_code == 200

    publish = client.post(
        "/v1/entries/upsert",
        headers=headers,
        json={
            "agent_id": "codex-laptop",
            "namespace": "chimera",
            "source_id": "runtime-snapshot",
            "kind": "artifact",
            "title": "Shannon runtime snapshot",
            "content": "The working shannon container uses claude-sonnet-4-6 and is baked into the image.",
            "tags": ["shannon", "runtime"],
            "metadata": {"captured": "2026-04-11"},
        },
    )
    assert publish.status_code == 200
    assert publish.json()["chunk_count"] >= 1

    search = client.post(
        "/v1/search",
        headers=headers,
        json={
            "query": "shannon container claude-sonnet-4-6",
            "namespace": "chimera",
            "requester_agent_id": "codex-laptop",
            "limit": 3,
        },
    )
    assert search.status_code == 200
    payload = search.json()
    assert len(payload["results"]) == 1
    assert payload["results"][0]["title"] == "Shannon runtime snapshot"


def test_direct_message_stays_in_recipient_inbox(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    headers = {"X-Shared-Key": "test-key"}

    for agent_id, display_name in [("codex-laptop", "Codex Laptop"), ("claude-desktop", "Claude Desktop")]:
        response = client.post(
            "/v1/agents/register",
            headers=headers,
            json={
                "agent_id": agent_id,
                "display_name": display_name,
                "device_name": agent_id,
                "tailscale_name": f"{agent_id}.tailnet.ts.net",
                "capabilities": ["notes"],
                "metadata": {},
            },
        )
        assert response.status_code == 200

    send = client.post(
        "/v1/entries/upsert",
        headers=headers,
        json={
            "agent_id": "codex-laptop",
            "namespace": "chimera",
            "source_id": "handoff-1",
            "kind": "message",
            "title": "Parity check",
            "content": "Compare the second machine against the runtime snapshot.",
            "recipient_id": "claude-desktop",
            "tags": ["handoff"],
            "metadata": {"priority": "high"},
        },
    )
    assert send.status_code == 200
    assert send.json()["visibility"] == "direct"

    claude_inbox = client.get("/v1/messages/inbox/claude-desktop?namespace=chimera", headers=headers)
    assert claude_inbox.status_code == 200
    assert len(claude_inbox.json()["messages"]) == 1
    assert claude_inbox.json()["messages"][0]["title"] == "Parity check"

    codex_inbox = client.get("/v1/messages/inbox/codex-laptop?namespace=chimera", headers=headers)
    assert codex_inbox.status_code == 200
    assert len(codex_inbox.json()["messages"]) == 0
