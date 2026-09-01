"""Relevance isolation: an agent must never be handed another project's context."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import publish, register

C3PO = "path:/home/ethanrisden/c3po"
RAMWARDEN = "path:/home/ethanrisden/ramwarden"


def seed(client: TestClient) -> None:
    register(client)
    publish(
        client, source_id="c3po-1", project_key=C3PO,
        title="c3po server URL",
        content="The manual testing folder points at the flask server on port 5000.",
        tags=["server"],
    )
    publish(
        client, source_id="ram-1", project_key=RAMWARDEN,
        title="ramwarden memory thresholds",
        content="The ramwarden daemon kills a browser tab when the server passes the memory threshold on port 5000.",
        tags=["server"],
    )


def test_project_scope_excludes_other_projects(client: TestClient) -> None:
    seed(client)
    payload = client.post(
        "/v1/search", json={"query": "server port 5000", "project_key": C3PO, "scope": "project"}
    ).json()

    assert payload["results"], "expected the project's own result"
    assert {r["project_key"] for r in payload["results"]} == {C3PO}
    assert payload["scope"]["projects_searched"] == [C3PO]
    joined = " ".join(r["content"] for r in payload["results"])
    assert "ramwarden" not in joined.lower()


def test_global_scope_must_be_asked_for_by_name(client: TestClient) -> None:
    seed(client)
    payload = client.post(
        "/v1/search", json={"query": "server port 5000", "project_key": C3PO, "scope": "global"}
    ).json()
    assert {r["project_key"] for r in payload["results"]} == {C3PO, RAMWARDEN}


def test_linked_scope_only_reaches_explicitly_linked_projects(client: TestClient) -> None:
    seed(client)

    before = client.post(
        "/v1/search", json={"query": "server port 5000", "project_key": C3PO, "scope": "linked"}
    ).json()
    assert {r["project_key"] for r in before["results"]} == {C3PO}

    assert client.post(
        "/v1/projects/link", json={"project_key": C3PO, "linked_project_key": RAMWARDEN}
    ).status_code == 200

    after = client.post(
        "/v1/search", json={"query": "server port 5000", "project_key": C3PO, "scope": "linked"}
    ).json()
    assert {r["project_key"] for r in after["results"]} == {C3PO, RAMWARDEN}


def test_relevance_floor_returns_guidance_not_filler(client: TestClient) -> None:
    """Below the floor the caller is told there is nothing, not handed junk.

    A weak-but-returned result is exactly what sends an agent off-track.
    """
    seed(client)
    payload = client.post(
        "/v1/search",
        json={"query": "kubernetes ingress certificate rotation", "project_key": C3PO, "scope": "project"},
    ).json()

    assert payload["results"] == []
    assert "No relevant prior context" in payload["message"]


def test_min_score_is_tunable_per_request(client: TestClient) -> None:
    seed(client)
    # Partially-covered query: "flask server" is present, the rest is not, so
    # term coverage lands strictly between 0 and 1 and the floor can bite.
    body = {
        "query": "flask server kubernetes ingress certificate rotation",
        "project_key": C3PO,
        "scope": "project",
    }

    permissive = client.post("/v1/search", json={**body, "min_score": 0.0}).json()
    strict = client.post("/v1/search", json={**body, "min_score": 0.99}).json()

    assert permissive["results"]
    assert 0.0 < permissive["results"][0]["term_coverage"] < 1.0
    assert strict["results"] == []
    assert strict["scope"]["min_score"] == 0.99


def test_results_report_absolute_relevance(client: TestClient) -> None:
    seed(client)
    payload = client.post(
        "/v1/search", json={"query": "manual testing folder flask", "project_key": C3PO}
    ).json()
    result = payload["results"][0]
    assert 0.0 <= result["relevance"] <= 1.0
    assert result["term_coverage"] > 0
    # Without NIM credentials retrieval is keyword-only and says so.
    assert payload["retrieval"]["vector_search_used"] is False
