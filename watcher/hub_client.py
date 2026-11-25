"""HTTP client to the memory hub (stdlib only, mirrors the CLI/MCP style)."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable


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


class ResolvingHubClient(HubClient):
    """A HubClient that re-picks its hub from an ordered candidate list.

    ``health()`` (which ``deliver`` calls before draining the spool) probes the
    candidates in preference order every cycle and points ``base_url`` at the
    first healthy one. So the watcher delivers to whichever configured hub is
    up, moves back to the preferred hub as soon as it returns, and -- when none
    answers, or none is configured yet -- reports unhealthy so the spool simply
    keeps growing.
    """

    def __init__(self, candidates: list[tuple[str, str]], shared_key: str = "",
                 timeout: float = 30.0, resolve: Callable[..., dict[str, Any]] | None = None) -> None:
        super().__init__(candidates[0][0] if candidates else "", shared_key, timeout)
        self.candidates = list(candidates)
        self._resolve = resolve
        self.selected: str | None = None
        self.last_tried: list[dict[str, Any]] = []

    @property
    def primary(self) -> str | None:
        return self.candidates[0][0].rstrip("/") if self.candidates else None

    def health(self) -> bool:
        if not self.candidates:  # no hub configured yet: stay offline, keep spooling
            self.selected = None
            self.last_tried = []
            return False
        if self._resolve is None:
            ok = super().health()
            self.selected = self.base_url if ok else None
            return ok
        result = self._resolve(self.candidates, key=self.shared_key, use_cache=False)
        self.last_tried = result.get("tried") or []
        selected = result.get("selected")
        selected = selected.rstrip("/") if selected else None
        if selected and selected != self.selected:
            note = "" if selected == self.primary else " (fallback; preferred hub is down)"
            print(f"[watcher] delivering to {selected}{note}", file=sys.stderr, flush=True)
        self.selected = selected
        if selected:
            self.base_url = selected
            return True
        return False
