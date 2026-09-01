"""Session summarisation and project briefs.

Summaries are the primary RAG unit: agents retrieve condensed session state,
not raw chat. When NIM is unavailable every function degrades to an extractive
summary built from transcript structure -- free, instant, and incapable of
hallucinating because it only re-states recorded facts.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .config import Config
from .nim import NimClient
from .store import index_chunks, utc_now
from .text import estimate_tokens, truncate_tokens

SYSTEM_PROMPT = (
    "You summarise software engineering sessions for other AI coding agents. "
    "Write only what the transcript supports; never invent file names, commands, or outcomes. "
    "Be terse and concrete. Prefer specific nouns: paths, commands, ports, error strings."
)

ROLLING_INSTRUCTIONS = """Summarise this in-progress coding session.

Respond with STRICT JSON only, no prose outside the object, no markdown fence:
{
  "summary": "<=120 words: what is being worked on, current state, immediate blocker if any>",
  "decisions": ["short statements of choices that were made and should be respected"],
  "open_threads": ["specific unfinished work items"]
}
If a list has nothing supported by the transcript, use []."""

FINAL_INSTRUCTIONS = """Summarise this completed coding session.

Respond with STRICT JSON only, no prose outside the object, no markdown fence:
{
  "summary": "<=160 words: what the session set out to do, what actually happened, how it ended",
  "decisions": ["choices made that future sessions should respect"],
  "open_threads": ["work explicitly left unfinished"]
}
If a list has nothing supported by the transcript, use []."""


def _rows_to_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def collect_session_facts(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """Structured facts about a session: prompts, replies, files, commands."""
    events = conn.execute(
        """
        SELECT seq, ts, role, kind, content, files_json, commands_json
        FROM session_events
        WHERE session_id = ? AND is_sidechain = 0
        ORDER BY seq ASC
        """,
        (session_id,),
    ).fetchall()

    prompts: list[str] = []
    replies: list[str] = []
    files: list[str] = []
    commands: list[str] = []

    for row in events:
        if row["kind"] == "prompt" and row["content"].strip():
            prompts.append(row["content"].strip())
        elif row["kind"] == "reply" and row["content"].strip():
            replies.append(row["content"].strip())
        for path in _rows_to_list(row["files_json"]):
            if path not in files:
                files.append(path)
        for command in _rows_to_list(row["commands_json"]):
            if command not in commands:
                commands.append(command)

    return {
        "events": events,
        "prompts": prompts,
        "replies": replies,
        "files": files,
        "commands": commands,
    }


def build_transcript(facts: dict[str, Any], *, max_chars: int = 14000) -> str:
    """Condense a session into an LLM-sized transcript.

    Keeps the first prompts (original intent) and the most recent activity,
    dropping the middle -- that is where a long session's low-signal bulk lives.
    """
    lines: list[str] = []
    for row in facts["events"]:
        content = (row["content"] or "").strip()
        if not content:
            continue
        label = {"prompt": "USER", "reply": "ASSISTANT", "tool_use": "TOOL", "meta": "META"}.get(
            row["kind"], row["kind"].upper()
        )
        lines.append(f"[{label}] {content}")

    if not lines:
        return ""

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text

    head_budget = max_chars // 3
    tail_budget = max_chars - head_budget
    head, tail, size = [], [], 0
    for line in lines:
        if size + len(line) > head_budget:
            break
        head.append(line)
        size += len(line) + 1
    size = 0
    for line in reversed(lines):
        if size + len(line) > tail_budget:
            break
        tail.append(line)
        size += len(line) + 1
    tail.reverse()
    return "\n".join(head + ["", "[... middle of session omitted ...]", ""] + tail)


def extractive_summary(session: sqlite3.Row, facts: dict[str, Any]) -> dict[str, Any]:
    """Build a summary from structure alone. Never calls a model."""
    title = session["ai_title"] or (facts["prompts"][0][:120] if facts["prompts"] else "Untitled session")
    parts: list[str] = [title.rstrip(".") + "."]

    if facts["prompts"]:
        parts.append(f"Opened with: {truncate_tokens(facts['prompts'][0], 40)}")
        if len(facts["prompts"]) > 1:
            parts.append(f"Most recent request: {truncate_tokens(facts['prompts'][-1], 40)}")
    if facts["files"]:
        shown = ", ".join(facts["files"][:8])
        more = f" (+{len(facts['files']) - 8} more)" if len(facts["files"]) > 8 else ""
        parts.append(f"Files touched: {shown}{more}.")
    if facts["commands"]:
        parts.append(f"Commands run: {'; '.join(facts['commands'][:5])}.")
    parts.append(
        f"{session['event_count']} events on {session['tool']}"
        + (f", branch {session['git_branch']}" if session["git_branch"] else "")
        + "."
    )

    return {
        "summary": " ".join(parts),
        "decisions": [],
        "open_threads": [],
        "files": facts["files"][:40],
        "model": "extractive",
    }


def _parse_model_json(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def generate_summary(
    conn: sqlite3.Connection,
    config: Config,
    nim: NimClient | None,
    session_id: str,
    tier: str = "rolling",
) -> dict[str, Any] | None:
    session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if session is None:
        return None

    facts = collect_session_facts(conn, session_id)
    if not facts["events"]:
        return None

    result = extractive_summary(session, facts)

    if nim is not None and nim.enabled:
        transcript = build_transcript(facts, max_chars=config.summary_transcript_chars)
        if transcript:
            instructions = ROLLING_INSTRUCTIONS if tier == "rolling" else FINAL_INSTRUCTIONS
            header = (
                f"Project: {session['project_key']}\n"
                f"Tool: {session['tool']}  Branch: {session['git_branch'] or 'n/a'}\n"
                f"Title: {session['ai_title'] or 'n/a'}\n\n"
            )
            raw = nim.complete(SYSTEM_PROMPT, f"{instructions}\n\n{header}TRANSCRIPT:\n{transcript}")
            parsed = _parse_model_json(raw) if raw else None
            if parsed and isinstance(parsed.get("summary"), str) and parsed["summary"].strip():
                result = {
                    "summary": parsed["summary"].strip(),
                    "decisions": [str(d) for d in parsed.get("decisions", []) if str(d).strip()][:12],
                    "open_threads": [str(t) for t in parsed.get("open_threads", []) if str(t).strip()][:12],
                    "files": facts["files"][:40],
                    "model": config.summary_model,
                }

    now = utc_now()
    conn.execute(
        """
        INSERT INTO session_summaries (
            session_id, tier, summary, decisions_json, open_threads_json,
            files_json, model, event_count, generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, tier) DO UPDATE SET
            summary = excluded.summary,
            decisions_json = excluded.decisions_json,
            open_threads_json = excluded.open_threads_json,
            files_json = excluded.files_json,
            model = excluded.model,
            event_count = excluded.event_count,
            generated_at = excluded.generated_at
        """,
        (
            session_id,
            tier,
            result["summary"],
            json.dumps(result["decisions"]),
            json.dumps(result["open_threads"]),
            json.dumps(result["files"]),
            result["model"],
            session["event_count"],
            now,
        ),
    )

    # Index the summary so it is retrievable. Decisions and open threads ride
    # along in the indexed text so a search for them hits this chunk.
    indexed_text = result["summary"]
    if result["decisions"]:
        indexed_text += "\nDecisions: " + "; ".join(result["decisions"])
    if result["open_threads"]:
        indexed_text += "\nOpen threads: " + "; ".join(result["open_threads"])
    if result["files"]:
        indexed_text += "\nFiles: " + ", ".join(result["files"][:20])

    index_chunks(
        conn,
        source_type="session_summary",
        source_ref=f"{session_id}:{tier}",
        project_key=session["project_key"],
        title=session["ai_title"] or f"Session {session_id[:8]}",
        chunks=[(f"sum:{session_id}:{tier}", indexed_text)],
        kind="session_summary",
        agent_id=session["agent_id"],
        session_id=session_id,
        tags=[session["tool"], tier],
        is_sidechain=bool(session["is_sidechain"]),
        ts=session["last_event_at"],
    )

    conn.execute(
        "UPDATE sessions SET summary_dirty = 0, summarized_event_count = ? WHERE session_id = ?",
        (session["event_count"], session_id),
    )
    return result


def project_brief(
    conn: sqlite3.Connection,
    config: Config,
    project_key: str,
    *,
    token_budget: int | None = None,
    max_sessions: int = 5,
    branch: str | None = None,
) -> dict[str, Any]:
    """Compose the session-start brief, newest first, under a hard token budget."""
    budget = token_budget or config.brief_token_budget
    project = conn.execute("SELECT * FROM projects WHERE project_key = ?", (project_key,)).fetchone()

    params: list[Any] = [project_key]
    branch_clause = ""
    if branch:
        branch_clause = " AND (s.git_branch = ? OR s.git_branch IS NULL)"
        params.append(branch)

    sessions = conn.execute(
        f"""
        SELECT s.*, sm.summary, sm.decisions_json, sm.open_threads_json, sm.tier, sm.model
        FROM sessions s
        LEFT JOIN session_summaries sm
          ON sm.session_id = s.session_id
         AND sm.tier = (
             SELECT tier FROM session_summaries
             WHERE session_id = s.session_id
             ORDER BY CASE tier WHEN 'final' THEN 0 ELSE 1 END
             LIMIT 1
         )
        WHERE s.project_key = ? AND s.is_sidechain = 0{branch_clause}
        ORDER BY s.last_event_at DESC
        LIMIT ?
        """,
        [*params, max_sessions],
    ).fetchall()

    decisions = conn.execute(
        """
        SELECT title, content, updated_at FROM entries
        WHERE kind = 'decision' AND entry_id IN (
            SELECT source_ref FROM memory_chunks
            WHERE source_type = 'entry' AND project_key = ?
        )
        ORDER BY updated_at DESC LIMIT 10
        """,
        (project_key,),
    ).fetchall()

    used = 0
    session_blocks: list[dict[str, Any]] = []
    open_threads: list[str] = []
    all_decisions: list[str] = [f"{row['title']}: {row['content']}" for row in decisions]

    for row in sessions:
        summary = row["summary"] or (row["ai_title"] or "").strip()
        if not summary:
            continue
        block = {
            "session_id": row["session_id"],
            "title": row["ai_title"],
            "tool": row["tool"],
            "branch": row["git_branch"],
            "status": row["status"],
            "last_event_at": row["last_event_at"],
            "summary": summary,
            "summary_tier": row["tier"],
            "summary_model": row["model"],
        }
        cost = estimate_tokens(summary) + 20
        if used + cost > budget and session_blocks:
            break
        used += cost
        session_blocks.append(block)
        open_threads.extend(_rows_to_list(row["open_threads_json"]))
        all_decisions.extend(_rows_to_list(row["decisions_json"]))

    # Deduplicate while preserving newest-first ordering.
    open_threads = list(dict.fromkeys(t for t in open_threads if t.strip()))[:10]
    all_decisions = list(dict.fromkeys(d for d in all_decisions if d.strip()))[:10]

    return {
        "project_key": project_key,
        "display_name": project["display_name"] if project else project_key.rsplit("/", 1)[-1],
        "repo_root": project["repo_root"] if project else None,
        "branch_filter": branch,
        "sessions": session_blocks,
        "open_threads": open_threads,
        "decisions": all_decisions,
        "session_count": len(session_blocks),
        "estimated_tokens": used,
        "token_budget": budget,
        "message": (
            None
            if session_blocks
            else "No prior sessions recorded for this project. Start fresh; do not assume prior context."
        ),
    }
