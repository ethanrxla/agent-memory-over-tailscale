from pathlib import Path

from fastapi.testclient import TestClient

from main import create_app
from scripts.generate_client_configs import slugify


def make_client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "agent-memory.db"
    app = create_app(db_path=str(db_path), shared_key="")
    return TestClient(app)


def seed_memory(client: TestClient) -> None:
    client.post(
        "/v1/agents/register",
        json={
            "agent_id": "codex-rxla",
            "display_name": "Codex RXLA",
            "device_name": "RXLA",
            "tailscale_name": "rxla.tailnet.ts.net",
            "capabilities": ["python", "mcp"],
            "metadata": {},
        },
    )
    client.post(
        "/v1/entries/upsert",
        json={
            "agent_id": "codex-rxla",
            "namespace": "chimera",
            "source_id": "boot-note",
            "kind": "artifact",
            "title": "Memory service started",
            "content": "Dashboard and MCP service are live on RXLA.",
            "tags": ["memory", "dashboard"],
        },
    )


def test_dashboard_renders_seeded_memory(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    seed_memory(client)

    response = client.get("/")
    assert response.status_code == 200
    assert "Agent Memory Dashboard" in response.text
    assert "Codex RXLA" in response.text
    assert "Memory service started" in response.text

    dashboard_json = client.get("/dashboard/data")
    assert dashboard_json.status_code == 200
    payload = dashboard_json.json()
    assert payload["stats"]["agents"] == 1
    assert payload["stats"]["entries"] == 1


def test_slugify_is_stable() -> None:
    assert slugify("RXLA Workstation") == "rxla-workstation"
