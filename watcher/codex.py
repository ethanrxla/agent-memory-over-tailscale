"""Parse Codex rollout transcripts into the common event stream.

Rollouts live under ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. The first
record is session_meta (session id, cwd). Content comes from top-level
``event_msg`` records -- current Codex CLI builds emit flat types like
``user_message``, ``agent_message``, ``exec_command_end``, ``patch_apply_end``,
``web_search_end`` and ``mcp_tool_call_end`` directly on the payload (no
``item_completed`` wrapper), which is what this module reads. Reasoning,
token-count, and turn-lifecycle records carry no durable retrieval signal and
are dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .common import ParsedEvent, ParsedSession

DEFAULT_ROOT = Path.home() / ".codex" / "sessions"


def find_transcripts(root: Path = DEFAULT_ROOT) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("**/rollout-*.jsonl"))


def _looks_like_boilerplate(text: str) -> bool:
    head = text.lstrip()[:40].lower()
    return head.startswith(("<app-context", "<recommended_plugins", "<environment_context"))


def parse_transcript(path: Path) -> tuple[ParsedSession | None, list[ParsedEvent]]:
    session: ParsedSession | None = None
    events: list[ParsedEvent] = []
    seq = 0

    for raw in _iter_lines(path):
        rtype = raw.get("type")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = raw.get("timestamp")

        if rtype == "session_meta":
            session = ParsedSession(
                session_id=payload.get("id") or payload.get("session_id") or path.stem,
                tool="codex",
                cwd=_strip_file_uri(payload.get("cwd")),
                started_at=payload.get("timestamp") or ts,
            )
            continue

        if rtype == "turn_context" and session is not None and not session.cwd:
            session.cwd = _strip_file_uri(payload.get("cwd"))
            continue

        if rtype != "event_msg":
            continue

        event = _event_to_event(payload, seq, ts)
        if event is None:
            continue
        events.append(event)
        if event.kind == "prompt" and session is not None:
            session.last_prompt = event.content
        seq += 1

    if session is None:
        session = ParsedSession(session_id=path.stem, tool="codex")
    return session, events


def _event_to_event(payload: dict[str, Any], seq: int, ts: str | None) -> ParsedEvent | None:
    etype = payload.get("type")

    if etype == "user_message":
        text = str(payload.get("message", "")).strip()
        if not text or _looks_like_boilerplate(text):
            return None
        return ParsedEvent(seq, "user", "prompt", content=text, ts=ts)

    if etype == "agent_message":
        text = str(payload.get("message", "")).strip()
        if not text:
            return None
        return ParsedEvent(seq, "assistant", "reply", content=text, ts=ts)

    if etype == "exec_command_end":
        command = payload.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        command = " ".join(str(command or "").split())
        if not command:
            return None
        return ParsedEvent(seq, "assistant", "tool_use", content=f"$ {command[:400]}",
                           commands=[command[:400]], ts=ts)

    if etype == "patch_apply_end":
        changes = payload.get("changes")
        files = list(changes.keys()) if isinstance(changes, dict) else []
        if not files:
            return None
        return ParsedEvent(seq, "assistant", "tool_use",
                           content="Edited: " + ", ".join(files[:8]), files=files, ts=ts)

    if etype == "web_search_end":
        query = payload.get("query") or (payload.get("action") or {}).get("query")
        if not query:
            return None
        return ParsedEvent(seq, "assistant", "tool_use",
                           content=f"web.search: {str(query)[:200]}", ts=ts)

    if etype == "mcp_tool_call_end":
        invocation = payload.get("invocation") or {}
        label = f"{invocation.get('server', 'mcp')}.{invocation.get('tool', 'call')}"
        return ParsedEvent(seq, "assistant", "tool_use", content=f"MCP {label}", ts=ts)

    # task_started/task_complete/token_count/context_compacted/turn_aborted/
    # view_image_tool_call and everything else: dropped.
    return None


def _strip_file_uri(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[len("file://"):] if text.startswith("file://") else text


def _iter_lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
