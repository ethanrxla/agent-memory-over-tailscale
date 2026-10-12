"""AGENT_MEMORY_URL can list several hubs; clients follow whichever is up."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_memory_hubs import candidate_urls, pick_hub
from agent_memory_mcp import AgentMemoryHttpClient
from watcher.hub_client import HubClient

DEAD = "http://127.0.0.1:1"


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"status": "ok", "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture()
def live_hub():
    server = HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_candidate_urls_splits_and_dedupes():
    raw = " http://a:8787/, http://b:8787\nhttp://a:8787 "
    assert candidate_urls(raw) == ["http://a:8787", "http://b:8787"]
    assert candidate_urls("") == ["http://127.0.0.1:8787"]


def test_pick_hub_skips_dead_hub(live_hub):
    assert pick_hub(f"{DEAD},{live_hub}", "") == live_hub


def test_pick_hub_falls_back_to_first_when_none_answer():
    assert pick_hub(f"{DEAD},http://127.0.0.1:2", "") == DEAD


def test_watcher_switches_to_live_hub(live_hub):
    client = HubClient(f"{DEAD},{live_hub}", timeout=1)
    assert client.health() is True
    assert client.base_url == live_hub


def test_mcp_client_repicks_after_connection_loss(live_hub):
    client = AgentMemoryHttpClient(base_url=f"{DEAD},{live_hub}", shared_key="")
    client._base_url = DEAD  # pretend the dead hub was up when first picked
    assert client.request_json("GET", "/v1/status")["path"] == "/v1/status"
    assert client.base_url == live_hub
