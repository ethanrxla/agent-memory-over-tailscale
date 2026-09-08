"""The hub distributes the skill, because it is the one thing every device reaches.

Devices are behind Tailscale with no inbound SSH, so pushing an updated
memory.py to each is not possible from the hub. Serving it makes updating a
device a single pull over the tailnet the agent already talks to.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_skill_files_are_served(client: TestClient) -> None:
    for name in ("SKILL.md", "memory.py"):
        r = client.get(f"/skill/agent-memory/{name}")
        assert r.status_code == 200, name
        assert r.text.strip()


def test_served_memory_py_is_the_real_client(client: TestClient) -> None:
    body = client.get("/skill/agent-memory/memory.py").text
    assert "def cmd_brief" in body
    assert "related_projects" in body, "must be the fixed client, not a stale copy"


def test_installer_script_is_served_and_runnable(client: TestClient) -> None:
    r = client.get("/skill/install.sh")
    assert r.status_code == 200
    assert r.text.startswith("#!")
    assert ".claude/skills/agent-memory" in r.text
    assert ".codex/skills/agent-memory" in r.text


def test_unknown_skill_files_are_refused(client: TestClient) -> None:
    assert client.get("/skill/agent-memory/secrets.env").status_code == 404


def test_path_traversal_is_refused(client: TestClient) -> None:
    for attempt in ("../../../etc/passwd", "..%2f..%2fmain.py", "%2e%2e%2fmain.py"):
        r = client.get(f"/skill/agent-memory/{attempt}")
        assert r.status_code in (404, 400), attempt
        assert "root:" not in r.text


def test_serving_respects_the_shared_key(keyed_client: TestClient) -> None:
    assert keyed_client.get("/skill/agent-memory/memory.py").status_code == 401
    ok = keyed_client.get(
        "/skill/agent-memory/memory.py", headers={"X-Shared-Key": "test-key"}
    )
    assert ok.status_code == 200
