"""Hermes Agent state.db adapter: parse, scrub, redact, checkpoint."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from watcher import hermes
from watcher.daemon import scan_hermes
from watcher.spool import Spool


def _make_db(path: Path, repo: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, cwd TEXT,
            git_repo_root TEXT, git_branch TEXT, started_at REAL, parent_session_id TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, tool_calls TEXT, timestamp REAL, _compressed_summary INTEGER DEFAULT 0);
        """
    )
    conn.execute("INSERT INTO sessions VALUES ('s1','discord','Fix onboarding',NULL,NULL,NULL,1795047016.0,NULL)")
    calls = [
        {"function": {"name": "terminal", "arguments": json.dumps({"command": f"cd {repo.as_posix()} && git status"})}},
        {"function": {"name": "write_file", "arguments": json.dumps({"path": str(repo / "app.py"), "content": "x"})}},
        {"function": {"name": "terminal", "arguments": json.dumps({"command": "export OPENAI_API_KEY=plantedsecret999"})}},
    ]
    rows = [
        ("s1", "user", "please fix the onboarding flow", None, 1795047017.0, 0),
        ("s1", "assistant", "On it.\nAuthorization: Bearer plantedtoken12345678901234\nDone soon.", json.dumps(calls), 1795047018.0, 0),
        ("s1", "tool", "huge tool output that must be dropped", None, 1795047019.0, 0),
        ("s1", "assistant", "compressed recap", None, 1795047020.0, 1),
        ("s1", "assistant", "Fixed. key was sk-proj-abcdefghijklmnopqrstuvwxyz0123", None, 1795047021.0, 0),
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp, _compressed_summary) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_parse_session_keeps_signal_drops_noise(tmp_path: Path) -> None:
    repo = tmp_path / "Users" / "me" / "code" / "proj"
    (repo / ".git").mkdir(parents=True)
    db = tmp_path / "state.db"
    _make_db(db, repo)
    conn = hermes.connect(db)
    row = hermes.list_sessions(conn)[0]
    session, events = hermes.parse_session(conn, row)
    conn.close()

    assert session.session_id == "hermes-s1"
    assert session.tool == "hermes"
    assert session.ai_title == "Fix onboarding"
    assert Path(session.cwd) == repo  # inferred from tool-call paths
    kinds = [e.kind for e in events]
    assert kinds[0] == "prompt" and "reply" in kinds and "tool_use" in kinds
    blob = json.dumps([e.__dict__ for e in events])
    assert "huge tool output" not in blob
    assert "compressed recap" not in blob
    assert "plantedsecret999" not in blob
    assert "plantedtoken" not in blob
    assert "sk-proj" not in blob
    assert str(repo / "app.py") in [f for e in events for f in e.files]
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_scrub_drops_credential_lines_only() -> None:
    text = "keep this\nNVIDIA_API_KEY=abc\nrisk-free task-runner\n-----BEGIN PRIVATE KEY-----"
    assert hermes.scrub(text) == "keep this\nrisk-free task-runner"


def test_scan_hermes_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "Users" / "me" / "code" / "proj"
    (repo / ".git").mkdir(parents=True)
    db = tmp_path / "state.db"
    _make_db(db, repo)
    spool = Spool(tmp_path / "spool.db")

    first = scan_hermes(db, spool, device="dev", agent_id=None, deny=[])
    assert first > 0
    assert scan_hermes(db, spool, device="dev", agent_id=None, deny=[]) == 0

    payloads = [json.loads(r["payload"]) for r in spool.next_batch()]
    assert {p["session"]["tool"] for p in payloads} == {"hermes"}
    assert "plantedsecret999" not in json.dumps(payloads)

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1','user','one more thing',1795047030.0)")
    conn.commit()
    conn.close()
    assert scan_hermes(db, spool, device="dev", agent_id=None, deny=[]) == 1
    spool.close()


def test_scan_hermes_respects_deny(tmp_path: Path) -> None:
    repo = tmp_path / "Users" / "me" / "code" / "proj"
    (repo / ".git").mkdir(parents=True)
    db = tmp_path / "state.db"
    _make_db(db, repo)
    spool = Spool(tmp_path / "spool.db")
    assert scan_hermes(db, spool, device="dev", agent_id=None, deny=[str(repo)]) == 0
    assert spool.pending_count() == 0
    spool.close()
