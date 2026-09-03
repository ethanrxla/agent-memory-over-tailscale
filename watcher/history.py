"""Recover orphaned sessions from prompt-input history.

Claude Code and Codex keep a flat log of the user's prompts (~/.claude/history.jsonl,
~/.codex/history.jsonl). Most of those sessions still have a full transcript and
are ingested from there. But when a transcript is deleted or rotated, the
prompts are all that survive -- a lower-fidelity but still useful record of what
was worked on and asked for. This module reconstructs those *orphaned* sessions
(prompt-only), skipping any session that already has a transcript so nothing is
duplicated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .common import ParsedEvent, ParsedSession

CLAUDE_HISTORY = Path.home() / ".claude" / "history.jsonl"
CODEX_HISTORY = Path.home() / ".codex" / "history.jsonl"

# Prompt-display strings that are UI machinery, not real user prose.
_SKIP_PREFIXES = ("/", "[Pasted text", "[Image", "[Request interrupted")


def _is_boilerplate(text: str) -> bool:
    t = text.strip()
    if not t or t.startswith(_SKIP_PREFIXES):
        return True
    # Terminal output pasted back in (shell prompt lines, sudo prompts).
    if "password for " in t or t.count("$ ") > 3:
        return True
    return False


def _ts_iso(value) -> str | None:
    if value is None:
        return None
    try:
        # Claude uses ms epoch, Codex uses s epoch.
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _iter_records(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _grouped(path: Path, id_key: str, text_key: str) -> dict[str, dict]:
    sessions: dict[str, dict] = {}
    for rec in _iter_records(path):
        sid = rec.get(id_key)
        text = rec.get(text_key)
        if not sid or not isinstance(text, str) or _is_boilerplate(text):
            continue
        entry = sessions.setdefault(sid, {"project": None, "prompts": []})
        if rec.get("project"):
            entry["project"] = rec["project"]
        entry["prompts"].append((_ts_iso(rec.get("timestamp") or rec.get("ts")), text.strip()))
    return sessions


def orphan_sessions(disk_session_ids: set[str]) -> Iterator[tuple[ParsedSession, list[ParsedEvent]]]:
    """Yield (session, prompt_events) for history sessions with no transcript.

    Claude history carries the project path; Codex history does not, so only
    Claude orphans get a real project scope (Codex orphans are rare and land in
    the global scope).
    """
    for path, id_key in ((CLAUDE_HISTORY, "sessionId"), (CODEX_HISTORY, "session_id")):
        tool = "claude-code" if "claude" in str(path) else "codex"
        for sid, data in _grouped(path, id_key, "display" if tool == "claude-code" else "text").items():
            if sid in disk_session_ids or not data["prompts"]:
                continue
            cwd = data["project"]
            first_ts = next((ts for ts, _ in data["prompts"] if ts), None)
            session = ParsedSession(
                session_id=sid,
                tool=tool,
                cwd=cwd,
                ai_title=data["prompts"][0][1][:80] if data["prompts"] else None,
                last_prompt=data["prompts"][-1][1] if data["prompts"] else None,
                started_at=first_ts,
            )
            events = [
                ParsedEvent(seq, "user", "prompt", content=text, ts=ts)
                for seq, (ts, text) in enumerate(data["prompts"])
            ]
            yield session, events
