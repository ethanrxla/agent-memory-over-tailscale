"""Regression tests for defects found in the original implementation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_memory.app import create_app
from agent_memory.retrieval import BM25_WEIGHTS
from agent_memory.text import build_fts_query
from tests.conftest import publish, register


def test_importing_main_does_not_touch_the_filesystem() -> None:
    """`main.py` used to run create_app() at import, mkdir-ing /data.

    That made the whole test suite fail to collect outside Docker.
    """
    import main

    assert callable(main.create_app)


def test_multiword_natural_query_matches_partial_documents(client: TestClient) -> None:
    """FTS terms are ORed, not ANDed.

    The original joined quoted tokens with spaces, which FTS5 reads as implicit
    AND, so a natural-language question only matched documents containing every
    single word -- in practice, nothing.
    """
    register(client)
    publish(
        client,
        source_id="s1",
        title="Shannon runtime container",
        content="We dropped the shannon runtime container because the baked image pinned an old model.",
        tags=["shannon"],
    )

    results = client.post(
        "/v1/search", json={"query": "why did we drop the shannon runtime container", "scope": "global"}
    ).json()["results"]
    assert len(results) == 1
    assert results[0]["title"] == "Shannon runtime container"


def test_fts_query_ors_terms_and_drops_stopwords() -> None:
    assert build_fts_query("the shannon runtime") == '"shannon" OR "runtime"'
    assert build_fts_query("   ") is None


def test_bm25_weight_count_matches_fts_column_count(tmp_path: Path) -> None:
    """One weight per FTS column.

    The original passed 7 weights to an 8-column table. SQLite accepts that
    silently, but the weights shift onto the wrong columns, so the intended
    title boost never applied.
    """
    app = create_app(db_path=str(tmp_path / "m.db"), shared_key="", worker_enabled=False)
    conn = sqlite3.connect(app.state.config.db_path)
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info(memory_chunks_fts)").fetchall()
    ]
    conn.close()
    assert len(BM25_WEIGHTS) == len(columns), (BM25_WEIGHTS, columns)


def test_title_match_outranks_body_match(client: TestClient) -> None:
    """The bm25 title boost has to actually take effect."""
    register(client)
    publish(client, source_id="body", title="Unrelated heading",
            content="Some prose that mentions telemetry once in passing.")
    publish(client, source_id="title", title="Telemetry pipeline design",
            content="Unrelated prose about breakfast.")

    results = client.post("/v1/search", json={"query": "telemetry", "scope": "global"}).json()["results"]
    assert results, "expected matches"
    assert results[0]["title"] == "Telemetry pipeline design"


def test_multichunk_entry_does_not_consume_the_limit(client: TestClient) -> None:
    """Several chunks of one entry collapse to a single result.

    Previously each matching chunk was its own row, so one long document could
    fill the caller's entire limit.
    """
    register(client)
    long_body = "\n\n".join(f"Paragraph {i} about kafka partitioning strategy." for i in range(12))
    publish(client, source_id="long", title="Kafka notes", content=long_body, chunk_size=300)
    publish(client, source_id="other", title="Kafka retention", content="Retention for kafka is 7 days.")

    payload = client.post("/v1/search", json={"query": "kafka", "scope": "global", "limit": 5}).json()
    titles = [r["title"] for r in payload["results"]]
    assert len(titles) == len(set(titles)) == 2
    long_result = next(r for r in payload["results"] if r["title"] == "Kafka notes")
    assert long_result["additional_chunks"] >= 1


def test_delete_entry_removes_all_index_artifacts(client: TestClient) -> None:
    register(client)
    entry = publish(client, source_id="tmp", title="Ephemeral note", content="delete me please")
    entry_id = entry["entry_id"]

    assert client.post("/v1/search", json={"query": "ephemeral", "scope": "global"}).json()["results"]

    assert client.delete(f"/v1/entries/{entry_id}").status_code == 200
    assert client.get(f"/v1/entries/{entry_id}").status_code == 404
    assert client.post("/v1/search", json={"query": "ephemeral", "scope": "global"}).json()["results"] == []

    status = client.get("/v1/status").json()
    assert status["counts"]["chunks"] == 0
    assert status["counts"]["embedding_queue"] == 0


def test_dashboard_rejects_key_in_query_string(keyed_client: TestClient) -> None:
    """A shared key must never be accepted from the URL.

    Query strings land in proxy access logs, browser history, and Referer
    headers. /login trades a key for a cookie and redirects it away.
    """
    assert keyed_client.get("/?key=test-key").status_code == 401
    assert keyed_client.get("/", headers={"X-Shared-Key": "test-key"}).status_code == 200

    login = keyed_client.get("/login?key=test-key", follow_redirects=False)
    assert login.status_code == 303
    assert keyed_client.get("/").status_code == 200  # cookie now set


def test_wrong_shared_key_is_rejected(keyed_client: TestClient) -> None:
    assert keyed_client.get("/health").status_code == 401
    assert keyed_client.get("/health", headers={"X-Shared-Key": "nope"}).status_code == 401
    assert keyed_client.get("/health", headers={"X-Shared-Key": "test-key"}).status_code == 200


def test_updated_at_index_exists(client: TestClient) -> None:
    conn = sqlite3.connect(client.app.state.config.db_path)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    conn.close()
    assert "idx_entries_updated_at" in names
