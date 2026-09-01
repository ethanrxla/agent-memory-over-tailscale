from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_memory.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Hub with the background worker off so tests stay deterministic."""
    app = create_app(db_path=str(tmp_path / "memory.db"), shared_key="", worker_enabled=False)
    return TestClient(app)


@pytest.fixture
def keyed_client(tmp_path: Path) -> TestClient:
    app = create_app(db_path=str(tmp_path / "memory.db"), shared_key="test-key", worker_enabled=False)
    return TestClient(app)


def register(client: TestClient, agent_id: str = "agent-1", **kwargs) -> None:
    payload = {"agent_id": agent_id, "display_name": kwargs.pop("display_name", agent_id)}
    payload.update(kwargs)
    response = client.post("/v1/agents/register", json=payload)
    assert response.status_code == 200


def publish(client: TestClient, **kwargs) -> dict:
    payload = {
        "agent_id": "agent-1",
        "namespace": "default",
        "title": "Untitled",
        "content": "content",
    }
    payload.update(kwargs)
    response = client.post("/v1/entries/upsert", json=payload)
    assert response.status_code == 200, response.text
    return response.json()
