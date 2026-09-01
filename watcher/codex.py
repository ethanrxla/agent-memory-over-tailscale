"""Parse Codex rollout transcripts into the common event stream.

Rollouts live under ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. The first
record is session_meta (session_id, cwd). The cleanest signal is the stream of
``event_msg/item_completed`` records, whose item type tells us what happened;
using those avoids double-counting the overlapping response_item/message
records and skips developer/app-context boilerplate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .common import ParsedEvent, ParsedSession

DEFAULT_ROOT = Path.home() / ".codex" / "sessions"

# Item content can be [{type:text|Text|input_text|output_text, text:...}].
_TEXT_TYPES = {"text", "Text", "input_text", "output_text"}


def find_transcripts(root: Path = DEFAULT_ROOT) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("**/rollout-*.jsonl"))


def _item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in _TEXT_TYPES:
                parts.append(str(block.get("text", "")).strip())
    return "\n".join(part for part in parts if part).strip()


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
                session_id=payload.get("session_id") or path.stem,
                tool="codex",
                cwd=_strip_file_uri(payload.get("cwd")),
                started_at=payload.get("timestamp") or ts,
            )
            continue

        if rtype == "turn_context" and session is not None and not session.cwd:
            session.cwd = _strip_file_uri(payload.get("cwd"))
            continue

        if rtype != "event_msg" or payload.get("type") != "item_completed":
            continue

        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        event = _item_to_event(item, seq, ts)
        if event is None:
            continue
        events.append(event)
        if event.kind == "prompt" and session is not None:
            session.last_prompt = event.content
        seq += 1

    if session is None:
        session = ParsedSession(session_id=path.stem, tool="codex")
    return session, events


def _item_to_event(item: dict[str, Any], seq: int, ts: str | None) -> ParsedEvent | None:
    itype = item.get("type")

    if itype == "UserMessage":
        text = _item_text(item)
        if not text or _looks_like_boilerplate(text):
            return None
        return ParsedEvent(seq, "user", "prompt", content=text, ts=ts)

    if itype == "AgentMessage":
        text = _item_text(item)
        if not text:
            return None
        return ParsedEvent(seq, "assistant", "reply", content=text, ts=ts)

    if itype == "CommandExecution":
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        command = " ".join(str(command or "").split())
        if not command:
            return None
        return ParsedEvent(seq, "assistant", "tool_use", content=f"$ {command[:400]}",
                           commands=[command[:400]], ts=ts)

    if itype == "FileChange":
        changes = item.get("changes")
        files = list(changes.keys()) if isinstance(changes, dict) else []
        if not files:
            return None
        return ParsedEvent(seq, "assistant", "tool_use",
                           content="Edited: " + ", ".join(files[:8]), files=files, ts=ts)

    if itype == "Extension":
        query = item.get("query") or (item.get("action") or {}).get("query")
        if not query:
            return None
        return ParsedEvent(seq, "assistant", "tool_use",
                           content=f"web.search: {str(query)[:200]}", ts=ts)

    if itype == "McpToolCall":
        label = f"{item.get('server', 'mcp')}.{item.get('tool', 'call')}"
        return ParsedEvent(seq, "assistant", "tool_use", content=f"MCP {label}", ts=ts)

    # Reasoning and everything else: dropped.
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
