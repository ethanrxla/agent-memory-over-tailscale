"""Embedding queue, worker drain, and hybrid vector retrieval with a fake NIM."""

from __future__ import annotations

import hashlib
import math

import pytest

from agent_memory.db import sqlite_vec_available
from agent_memory.worker import drain_embeddings
from tests.conftest import publish, register

pytestmark = pytest.mark.skipif(not sqlite_vec_available(), reason="sqlite-vec not installed")


class FakeNim:
    """Deterministic embeddings: hash text into a small unit vector.

    Keeps vector search meaningful in tests -- similar text hashes near itself --
    without calling the network.
    """

    enabled = True
    last_error = None

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts, input_type="passage"):
        return [self._vec(t) for t in texts]


def test_publish_enqueues_for_embedding(client) -> None:
    register(client)
    publish(client, source_id="e1", title="Vector note", content="content about distributed tracing")
    status = client.get("/v1/status").json()
    assert status["counts"]["embedding_queue"] >= 1
    assert status["counts"]["embedded_chunks"] == 0  # no NIM yet


def test_worker_embeds_and_vector_search_runs(client, monkeypatch) -> None:
    register(client)
    publish(client, source_id="e1", project_key="path:/p", title="Distributed tracing",
            content="We use opentelemetry spans to trace requests across services.")
    publish(client, source_id="e2", project_key="path:/p", title="Breakfast",
            content="The best pancakes use buttermilk and a hot griddle.")

    ctx = client.app.state.ctx
    fake = FakeNim(ctx.config.embed_dim)
    conn = ctx.open()
    try:
        stats = drain_embeddings(conn, ctx.config, fake)
        conn.commit()
    finally:
        conn.close()
    assert stats["embedded"] >= 2

    status = client.get("/v1/status").json()
    assert status["counts"]["embedded_chunks"] >= 2
    assert status["embedding_coverage"] > 0

    # With embeddings present the query is embedded too; monkeypatch the query side.
    monkeypatch.setattr(ctx, "query_vector", lambda q: fake.embed([q], input_type="query")[0])
    payload = client.post("/v1/search", json={
        "query": "opentelemetry span tracing", "project_key": "path:/p", "scope": "project"}).json()
    assert payload["retrieval"]["vector_search_used"] is True
    assert payload["results"][0]["title"] == "Distributed tracing"
    assert payload["results"][0]["similarity"] is not None


def test_dim_mismatch_is_rejected_not_stored(client) -> None:
    """A model returning the wrong width must not poison the vector table."""
    register(client)
    publish(client, source_id="e1", title="note", content="some content here")

    class WrongDim(FakeNim):
        def embed(self, texts, input_type="passage"):
            return [[0.1, 0.2, 0.3] for _ in texts]  # 3 dims, not embed_dim

    ctx = client.app.state.ctx
    conn = ctx.open()
    try:
        stats = drain_embeddings(conn, ctx.config, WrongDim(ctx.config.embed_dim))
        conn.commit()
    finally:
        conn.close()
    assert stats["skipped"] >= 1
    assert stats["embedded"] == 0
