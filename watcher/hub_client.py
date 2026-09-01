"""HTTP client to the memory hub (stdlib only, mirrors the CLI/MCP style)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HubClient:
    def __init__(self, base_url: str, shared_key: str = "", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
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
        try:
            result = self._request("GET", "/health")
            return result.get("status") == "ok"
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False

    def send_events(self, session: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/v1/sessions/events", {"session": session, "events": events})

    def register_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/agents/register", payload)
