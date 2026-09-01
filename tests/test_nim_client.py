"""NimClient request shaping and degradation (no network)."""

from __future__ import annotations

from agent_memory.config import load_config
from agent_memory.nim import NimClient, strip_reasoning


class _Capture(NimClient):
    """Intercept _post so we can assert the outgoing payload without a network."""

    def __init__(self, config, response):
        super().__init__(config)
        self.captured = None
        self._response = response

    def _post(self, path, payload, timeout=None):
        self.captured = (path, payload)
        self.captured_timeout = timeout
        return self._response


def _cfg(**kw):
    return load_config(nvidia_api_key="test-key", **kw)


def test_summary_extra_body_is_sent() -> None:
    cfg = _cfg(summary_model="deepseek-ai/deepseek-v4-flash-0731",
              summary_extra_body={"chat_template_kwargs": {"thinking": False}})
    client = _Capture(cfg, {"choices": [{"message": {"content": "hello"}}]})
    assert client.complete("sys", "user") == "hello"
    path, payload = client.captured
    assert path == "/chat/completions"
    assert payload["model"] == "deepseek-ai/deepseek-v4-flash-0731"
    assert payload["chat_template_kwargs"] == {"thinking": False}
    assert payload["stream"] is False


def test_kimi_default_sends_no_extra_body() -> None:
    cfg = _cfg()  # default summary_model is kimi-k3, extra_body empty
    client = _Capture(cfg, {"choices": [{"message": {"content": "ok"}}]})
    client.complete("s", "u")
    _, payload = client.captured
    assert payload["model"] == "moonshotai/kimi-k3"
    assert "chat_template_kwargs" not in payload


def test_falls_back_to_reasoning_content_when_content_empty() -> None:
    """Reasoning models sometimes leave content empty and fill reasoning_content."""
    cfg = _cfg()
    client = _Capture(cfg, {"choices": [{"message": {"content": "", "reasoning_content": "the answer"}}]})
    assert client.complete("s", "u") == "the answer"


def test_disabled_without_key() -> None:
    client = NimClient(load_config())  # no key
    assert client.enabled is False
    assert client.complete("s", "u") is None
    assert client.embed(["x"]) is None


def test_strip_reasoning_removes_think_blocks() -> None:
    assert strip_reasoning("<think>reasoning</think>Answer.") == "Answer."
    assert strip_reasoning("no tags") == "no tags"


def test_summary_uses_the_longer_summary_timeout() -> None:
    """Summaries get summary_timeout, not the shorter interactive nim_timeout."""
    cfg = _cfg(summary_timeout=120.0, nim_timeout=60.0)
    client = _Capture(cfg, {"choices": [{"message": {"content": "x"}}]})
    client.complete("s", "u")
    assert client.captured_timeout == 120.0
