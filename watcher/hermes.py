"""Read Hermes Agent sessions into the common event stream.

Hermes keeps every conversation (CLI, Discord/gateway, cron, delegated
sub-agents) in one SQLite file, ``state.db``:

    Windows: %LOCALAPPDATA%\\hermes\\state.db
    Linux/macOS: ~/.hermes/state.db          (override with HERMES_HOME)

``sessions`` holds one row per conversation (id, source, title, cwd,
git_repo_root, started_at, parent_session_id) and ``messages`` holds the turns
(role user/assistant/tool, content, tool_calls JSON). As with the transcript
parsers, only durable signal is kept: user prompts, assistant replies, and
compact tool facts (commands run, files touched). Tool results, reasoning and
compression summaries are dropped.

The database is opened read-only. Event ``seq`` is derived from the message
row id (``id * 10 + k``), which is stable and monotonic, so the daemon's
``last_seq`` checkpoint makes re-scans idempotent.

Hermes transcripts carry far more raw shell output and config than Claude
Code transcripts, so on top of the hub redactor every line that looks like it
holds a credential (``*_KEY=``, ``Bearer ``, ``sk-``, private-key armour, ...)
is dropped before the event leaves this module.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import ParsedEvent, ParsedSession

SESSION_PREFIX = "hermes-"
TOOL_NAME = "hermes"


def default_db() -> Path:
    home = os.getenv("HERMES_HOME")
    if home:
        return Path(home) / "state.db"
    if sys.platform == "win32" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "hermes" / "state.db"
    return Path.home() / ".hermes" / "state.db"


# ---------------------------------------------------------------- scrubbing

_SENSITIVE_LINE = re.compile(
    r"(?i)(_KEY\s*[=:]|_TOKEN\s*[=:]|_SECRET\s*[=:]|PASSWORD\s*[=:]|bearer\s|"
    r"\bsk-[A-Za-z0-9]|BEGIN [A-Z ]*PRIVATE KEY|x-shared-key|api[_-]?key\s*[=:])"
)


def scrub(text: str) -> str:
    """Drop whole lines that look like they carry a credential."""
    if not text:
        return ""
    return "\n".join(line for line in text.splitlines() if not _SENSITIVE_LINE.search(line)).strip()


# ---------------------------------------------------------------- tool calls

_FILE_TOOLS = {"read_file", "write_file", "patch"}
_SHELL_TOOLS = {"terminal"}


def _tool_event(name: str, args: dict[str, Any], seq: int, ts: str | None) -> ParsedEvent | None:
    files: list[str] = []
    commands: list[str] = []
    content = ""
    if name in _FILE_TOOLS:
        path = args.get("path")
        if path:
            files.append(str(path))
            content = f"{name} {path}"
    elif name in _SHELL_TOOLS:
        command = scrub(str(args.get("command") or ""))
        command = " ".join(command.split())[:400]
        if command:
            commands.append(command)
            content = f"$ {command}"
    elif name in {"web_search", "web_extract"}:
        target = args.get("query") or args.get("urls")
        if target:
            content = f"{name}: {str(target)[:200]}"
    elif name == "delegate_task":
        goal = scrub(str(args.get("goal") or args.get("task") or ""))
        content = f"Delegated to sub-agent: {goal[:200]}" if goal else "Delegated to sub-agent"
    elif name:
        content = f"{name} called"
    if not (content or files or commands):
        return None
    return ParsedEvent(seq, "assistant", "tool_use", content=content, files=files, commands=commands, ts=ts)


def _parse_tool_calls(raw: str | None) -> list[tuple[str, dict[str, Any]]]:
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for call in calls if isinstance(calls, list) else []:
        fn = (call or {}).get("function") or {}
        args: Any = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        out.append((str(fn.get("name") or ""), args if isinstance(args, dict) else {}))
    return out


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:  # multimodal content stored as a JSON block list
                return _text(json.loads(stripped))
            except json.JSONDecodeError:
                return stripped
        return stripped
    if isinstance(content, list):
        parts = [str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") in {"text", "input_text", "output_text"}]
        return "\n".join(p.strip() for p in parts if p.strip())
    return str(content)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


# ---------------------------------------------------------------- projects

_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'`;|&<>()*?]+")
_MSYS_PATH = re.compile(r"(?<![\w:])/([a-zA-Z])/(Users/[^\s\"'`;|&<>()*?]+)")


def _git_root(path_text: str, cache: dict[str, str | None]) -> str | None:
    path = Path(path_text.replace("\\", "/").rstrip("/.,:"))
    for candidate in [path, *path.parents]:
        key = str(candidate)
        if key in cache:
            return cache[key]
        if len(candidate.parts) <= 3:  # stop at drive root / C:/Users/<name>
            cache[key] = None
            return None
        try:
            if (candidate / ".git").exists():
                cache[key] = str(candidate)
                return cache[key]
        except OSError:
            pass
    return None


_SKIP_PARTS = ("/appdata/local/hermes/", "/.hermes/", "/appdata/local/temp/hermes")


def infer_project_dir(texts: list[str], skip: tuple[str, ...] = _SKIP_PARTS) -> str | None:
    """Pick the git repo most referenced by a session's tool calls."""
    cache: dict[str, str | None] = {}
    votes: Counter[str] = Counter()
    for text in texts:
        found = _WIN_PATH.findall(text) + [f"{d.upper()}:/{rest}" for d, rest in _MSYS_PATH.findall(text)]
        for raw in found:
            norm = raw.replace("\\", "/").lower() + "/"
            if any(part in norm for part in skip):
                continue
            root = _git_root(raw, cache)
            if root:
                votes[root] += 1
    return votes.most_common(1)[0][0] if votes else None


# ---------------------------------------------------------------- sessions


def connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def list_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, source, title, cwd, git_repo_root, git_branch, started_at, parent_session_id "
        "FROM sessions ORDER BY started_at"
    ).fetchall()


def max_message_id(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ?", (session_id,)).fetchone()
    return int(row[0])


def parse_session(conn: sqlite3.Connection, row: sqlite3.Row,
                  fallback_cwd: str | None = None) -> tuple[ParsedSession, list[ParsedEvent]]:
    session = ParsedSession(
        session_id=SESSION_PREFIX + row["id"],
        tool=TOOL_NAME,
        cwd=row["git_repo_root"] or row["cwd"],
        git_branch=row["git_branch"],
        ai_title=scrub(row["title"] or "")[:400] or None,
        started_at=_iso(row["started_at"]),
    )
    events: list[ParsedEvent] = []
    path_texts: list[str] = []
    messages = conn.execute(
        "SELECT id, role, content, tool_calls, timestamp FROM messages "
        "WHERE session_id = ? AND COALESCE(_compressed_summary, 0) = 0 ORDER BY id",
        (row["id"],),
    ).fetchall()
    for msg in messages:
        base = int(msg["id"]) * 10
        ts = _iso(msg["timestamp"])
        role = msg["role"]
        if role == "user":
            text = scrub(_text(msg["content"]))
            if text:
                events.append(ParsedEvent(base, "user", "prompt", content=text, ts=ts))
                session.last_prompt = text
        elif role == "assistant":
            text = scrub(_text(msg["content"]))
            if text:
                events.append(ParsedEvent(base, "assistant", "reply", content=text, ts=ts))
            for k, (name, args) in enumerate(_parse_tool_calls(msg["tool_calls"])[:9], start=1):
                for key in ("path", "workdir", "command", "code"):
                    if isinstance(args.get(key), str):
                        path_texts.append(args[key])
                event = _tool_event(name, args, base + k, ts)
                if event is not None:
                    events.append(event)
        # tool results, session_meta and reasoning are intentionally dropped.
    if not session.cwd:
        session.cwd = infer_project_dir(path_texts) or fallback_cwd
    if not session.ai_title or session.ai_title.startswith("Subagent:"):
        # Delegated sub-agent titles are just "Subagent: CONTEXT: #n"; lift the task line.
        first = next((e.content for e in events if e.kind == "prompt"), "")
        match = re.search(r"TASK:\s*(\S.*)", first)
        line = match.group(1) if match else next((ln for ln in first.splitlines() if ln.strip()), "")
        if line:
            prefix = "Subagent: " if row["source"] == "subagent" else ""
            session.ai_title = (prefix + line.strip())[:200]
    return session, events
