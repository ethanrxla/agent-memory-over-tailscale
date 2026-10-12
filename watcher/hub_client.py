"""HTTP client to the memory hub (stdlib only, mirrors the CLI/MCP style)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from agent_memory_hubs import candidate_urls, is_healthy


class HubClient:
    def __init__(self, base_url: str, shared_key: str = "", timeout: float = 30.0) -> None:
        # base_url may list several hubs (comma separated); health() follows
        # whichever one is up, so a dual-boot hub host does not strand the spool.
        self.candidates = candidate_urls(base_url)
        self.base_url = self.candidates[0]
        self.shared_key = shared_key
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, method=method, data=data)
        request.add_header("Content-Type", "application/json")
        if self.shared_key:
            request.add_header("X-Shared-Key", self.shared_key)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def health(self) -> bool:
        if is_healthy(self.base_url, self.shared_key):
            return True
        for url in self.candidates:
            if url != self.base_url and is_healthy(url, self.shared_key):
                print(f"[watcher] hub {self.base_url} unreachable; switching to {url}", flush=True)
                self.base_url = url
                return True
        return False

    def send_events(self, session: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/v1/sessions/events", {"session": session, "events": events})

    def register_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/agents/register", payload)
