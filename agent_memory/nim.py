"""NVIDIA NIM client (OpenAI-compatible) for embeddings and summarisation.

Every call is best-effort: on missing credentials, HTTP failure, or timeout the
caller gets None and the hub falls back to FTS5-only retrieval and extractive
summaries. NIM is an enhancement here, never a dependency.

Uses urllib rather than a new HTTP dependency, matching the CLI and MCP wrapper.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Config


class RateLimiter:
    """Token bucket shared across NIM calls.

    The free tier allows roughly 40 requests/minute/model; the default keeps
    a margin under that so a backfill cannot get the key throttled.
    """

    def __init__(self, rpm: int) -> None:
        self.capacity = max(1, rpm)
        self.tokens = float(self.capacity)
        self.refill_per_sec = self.capacity / 60.0
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    float(self.capacity),
                    self.tokens + (now - self.updated) * self.refill_per_sec,
                )
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                needed = (1.0 - self.tokens) / self.refill_per_sec
            if time.monotonic() + needed > deadline:
                return False
            time.sleep(min(needed, 1.0))


class NimClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.limiter = RateLimiter(config.nim_rpm)
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self.config.nim_enabled

    def _post(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            self.last_error = "NVIDIA_API_KEY is not set"
            return None
        if not self.limiter.acquire():
            self.last_error = "rate limiter timeout"
            return None

        url = self.config.nvidia_base_url.rstrip("/") + path
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.nvidia_api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.nim_timeout) as response:
                self.last_error = None
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            self.last_error = f"HTTP {exc.code}: {body}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.last_error = f"request failed: {exc}"
        except json.JSONDecodeError as exc:
            self.last_error = f"bad JSON from NIM: {exc}"
        return None

    def embed(self, texts: list[str], *, input_type: str = "passage") -> list[list[float]] | None:
        """Embed a batch. input_type must be 'passage' when indexing, 'query' when searching."""
        if not texts:
            return []
        payload = {
            "model": self.config.embed_model,
            "input": texts,
            "encoding_format": "float",
            "input_type": input_type,
            "truncate": "END",
        }
        data = self._post("/embeddings", payload)
        if not data:
            return None
        try:
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            vectors = [list(map(float, item["embedding"])) for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            self.last_error = f"unexpected embeddings response: {exc}"
            return None
        if len(vectors) != len(texts):
            self.last_error = f"expected {len(texts)} embeddings, got {len(vectors)}"
            return None
        return vectors

    def complete(self, system: str, user: str, *, max_tokens: int = 700, temperature: float = 0.2) -> str | None:
        payload = {
            "model": self.config.summary_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        # Models vary in how thinking is toggled; the per-model flags live in
        # config so switching summary models needs no code change. Disabling
        # thinking keeps a background summariser fast and its output clean.
        if self.config.summary_extra_body:
            payload.update(self.config.summary_extra_body)
        data = self._post("/chat/completions", payload, timeout=self.config.summary_timeout)
        if not data:
            return None
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
            # Some reasoning models leave content empty and put the answer in
            # reasoning_content; fall back to it rather than returning nothing.
            if not (isinstance(content, str) and content.strip()):
                content = message.get("reasoning_content") or message.get("reasoning")
        except (KeyError, IndexError, TypeError) as exc:
            self.last_error = f"unexpected chat response: {exc}"
            return None
        if not isinstance(content, str) or not content.strip():
            self.last_error = "empty completion"
            return None
        return strip_reasoning(content).strip()


def strip_reasoning(text: str) -> str:
    """Drop <think> blocks that reasoning models emit before the answer."""
    import re

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"^.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip() or text.strip()
