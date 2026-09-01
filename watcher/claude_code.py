"""Parse Claude Code JSONL transcripts into the common event stream.

Each project lives under ~/.claude/projects/<slug>/<session-uuid>.jsonl. Records
carry cwd, gitBranch, sessionId and isSidechain, plus free ai-title and
last-prompt records we lift as session metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .common import ParsedEvent, ParsedSession, summarize_tool_use

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

# Record types that carry no durable signal for retrieval.
_DROP_TYPES = {
    "attachment", "file-history-snapshot", "mode", "permission-mode",
    "atis-latch", "bridge-session", "queue-operation", "ai-title-changed",
}


def find_transcripts(root: Path = DEFAULT_ROOT) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("*/*.jsonl"))


def _text_blocks(content: Any) -> tuple[str, list[ParsedEvent]]:
    """Return (joined text, tool-use events) from a message content field."""
    if isinstance(content, str):
        return content.strip(), []
    text_parts: list[str] = []
    tool_events: list[ParsedEvent] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text", "")).strip())
            elif btype == "tool_use":
                event = summarize_tool_use(block.get("name", "tool"), block.get("input") or {})
                if event is not None:
                    tool_events.append(event)
            # thinking / tool_result blocks are intentionally dropped.
    return "\n".join(part for part in text_parts if part).strip(), tool_events


# Wrapper tags Claude Code injects around slash-command invocations, command
# output, and system notifications -- machinery, not user prose.
_COMMAND_MARKERS = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-stdout>", "<local-command-caveat>",
    "<task-notification>", "<system-reminder>",
)


def _is_command_boilerplate(text: str) -> bool:
    head = text.lstrip()
    return head.startswith(_COMMAND_MARKERS)


def _user_text(content: Any) -> str:
    """Extract only genuine user prose.

    Skips tool_result echoes and the XML-wrapped slash-command / notification
    machinery Claude Code threads through the user role, which would otherwise
    surface as spurious "prompts" and pollute summaries.
    """
    if isinstance(content, str):
        text = content.strip()
        return "" if _is_command_boilerplate(text) else text
    if isinstance(content, list):
        parts = [
            str(block.get("text", "")).strip()
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part).strip()
        return "" if _is_command_boilerplate(text) else text
    return ""


def parse_transcript(path: Path) -> tuple[ParsedSession | None, list[ParsedEvent]]:
    session: ParsedSession | None = None
    events: list[ParsedEvent] = []
    seq = 0

    session_id = path.stem
    for raw in _iter_lines(path):
        rtype = raw.get("type")
        if rtype in _DROP_TYPES:
            continue

        if session is None and rtype in {"user", "assistant", "system", "summary"}:
            session = ParsedSession(
                session_id=raw.get("sessionId") or session_id,
                tool="claude-code",
                cwd=raw.get("cwd"),
                git_branch=raw.get("gitBranch"),
                started_at=raw.get("timestamp"),
                is_sidechain=bool(raw.get("isSidechain")),
            )

        if rtype == "ai-title" and session is not None:
            session.ai_title = raw.get("aiTitle") or session.ai_title
            continue
        if rtype == "last-prompt" and session is not None:
            session.last_prompt = raw.get("lastPrompt") or session.last_prompt
            continue

        ts = raw.get("timestamp")
        is_side = bool(raw.get("isSidechain"))
        if session is not None:
            if raw.get("cwd") and not session.cwd:
                session.cwd = raw.get("cwd")
            if raw.get("gitBranch") and not session.git_branch:
                session.git_branch = raw.get("gitBranch")

        if rtype == "user":
            if raw.get("isMeta"):
                continue
            text = _user_text((raw.get("message") or {}).get("content"))
            if text:
                events.append(ParsedEvent(seq, "user", "prompt", content=text, is_sidechain=is_side, ts=ts))
                seq += 1
                if session and not is_side:
                    session.last_prompt = text
        elif rtype == "assistant":
            text, tool_events = _text_blocks((raw.get("message") or {}).get("content"))
            if text:
                events.append(ParsedEvent(seq, "assistant", "reply", content=text, is_sidechain=is_side, ts=ts))
                seq += 1
            for event in tool_events:
                event.seq = seq
                event.is_sidechain = is_side
                event.ts = ts
                events.append(event)
                seq += 1

    return session, events


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
