"""Orphaned-session recovery from prompt-input history."""

from __future__ import annotations

import json
from pathlib import Path

from watcher import history
from watcher.common import ParsedEvent


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_orphan_recovery_skips_boilerplate_and_ingested(tmp_path: Path, monkeypatch) -> None:
    claude = tmp_path / "claude_history.jsonl"
    _write(claude, [
        # orphaned session in a real project -> recovered
        {"sessionId": "orphan-1", "project": "/home/x/proj", "timestamp": 1775461054930, "display": "add auth to the api"},
        {"sessionId": "orphan-1", "project": "/home/x/proj", "timestamp": 1775461154930, "display": "now write tests"},
        # boilerplate -> skipped
        {"sessionId": "orphan-1", "project": "/home/x/proj", "timestamp": 1775461254930, "display": "/init"},
        {"sessionId": "orphan-1", "project": "/home/x/proj", "timestamp": 1775461354930, "display": "[Pasted text #3 +68 lines]"},
        # session that HAS a transcript -> not an orphan, skipped entirely
        {"sessionId": "has-transcript", "project": "/home/x/proj", "timestamp": 1775461454930, "display": "hello"},
    ])
    monkeypatch.setattr(history, "CLAUDE_HISTORY", claude)
    monkeypatch.setattr(history, "CODEX_HISTORY", tmp_path / "nonexistent.jsonl")

    orphans = list(history.orphan_sessions(disk_session_ids={"has-transcript"}))
    assert len(orphans) == 1
    session, events = orphans[0]
    assert session.session_id == "orphan-1"
    assert session.tool == "claude-code"
    assert session.cwd == "/home/x/proj"
    # only the two real prompts survive; /init and paste placeholder dropped
    assert [e.content for e in events] == ["add auth to the api", "now write tests"]
    assert all(isinstance(e, ParsedEvent) and e.kind == "prompt" for e in events)
    assert session.last_prompt == "now write tests"


def test_codex_history_without_project(tmp_path: Path, monkeypatch) -> None:
    codex = tmp_path / "codex_history.jsonl"
    _write(codex, [
        {"session_id": "cx-orphan", "ts": 1775457887, "text": "install docker on pop os"},
    ])
    monkeypatch.setattr(history, "CLAUDE_HISTORY", tmp_path / "none.jsonl")
    monkeypatch.setattr(history, "CODEX_HISTORY", codex)

    orphans = list(history.orphan_sessions(disk_session_ids=set()))
    assert len(orphans) == 1
    session, events = orphans[0]
    assert session.tool == "codex"
    assert session.cwd is None  # codex history carries no project
    assert events[0].content == "install docker on pop os"
