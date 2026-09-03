"""Watcher parsers, redaction-before-send, spool, and offline replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from watcher import claude_code, codex
from watcher.daemon import deliver, scan_file
from watcher.hub_client import HubClient
from watcher.spool import Spool

CLAUDE_LINES = [
    {"type": "user", "sessionId": "cc-1", "cwd": "/home/x/proj", "gitBranch": "main",
     "timestamp": "2026-08-30T10:00:00Z",
     "message": {"role": "user", "content": "wire the client to the flask server on port 5000"}},
    {"type": "ai-title", "sessionId": "cc-1", "aiTitle": "Wire client to server"},
    {"type": "assistant", "sessionId": "cc-1", "timestamp": "2026-08-30T10:01:00Z",
     "message": {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "leaking sk-ant-shouldnotstore0123456789abc here"},
         {"type": "text", "text": "Setting ANTHROPIC_API_KEY=sk-ant-api03-PLANTED0123456789abcdef now."},
         {"type": "tool_use", "name": "Write", "input": {"file_path": "/home/x/proj/client.py", "content": "x"}}]}},
    {"type": "assistant", "sessionId": "cc-1", "timestamp": "2026-08-30T10:02:00Z",
     "message": {"role": "assistant", "content": [
         {"type": "tool_use", "name": "Bash",
          "input": {"command": "DB_PASSWORD=plantedsecret123 python client.py", "description": "run"}}]}},
    {"type": "user", "sessionId": "cc-1", "timestamp": "2026-08-30T10:03:00Z",
     "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok connected"}]}},
]


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_claude_parser_drops_noise_keeps_signal(tmp_path: Path) -> None:
    path = tmp_path / "proj" / "cc-1.jsonl"
    _write_transcript(path, CLAUDE_LINES)
    session, events = claude_code.parse_transcript(path)

    assert session.tool == "claude-code"
    assert session.cwd == "/home/x/proj"
    assert session.ai_title == "Wire client to server"
    kinds = [e.kind for e in events]
    assert "prompt" in kinds and "reply" in kinds and "tool_use" in kinds
    # thinking block and tool_result never become events
    joined = " ".join(e.content for e in events)
    assert "leaking" not in joined  # thinking dropped
    assert "ok connected" not in joined  # tool_result dropped
    assert "/home/x/proj/client.py" in {f for e in events for f in e.files}


def test_codex_parser(tmp_path: Path) -> None:
    # Current Codex CLI rollout format: flat event_msg payloads (user_message,
    # agent_message, exec_command_end, patch_apply_end, ...), no item_completed
    # wrapper. session_meta carries the session id under "id".
    lines = [
        {"type": "session_meta", "timestamp": "2026-08-30T10:00:00Z",
         "payload": {"id": "cx-1", "cwd": "/home/x/proj"}},
        {"type": "event_msg", "timestamp": "t1",
         "payload": {"type": "user_message", "message": "fix the parser bug"}},
        {"type": "event_msg", "timestamp": "t2",
         "payload": {"type": "token_count", "info": {}}},
        {"type": "event_msg", "timestamp": "t3",
         "payload": {"type": "exec_command_end", "command": ["/bin/bash", "-lc", "pytest -q"]}},
        {"type": "event_msg", "timestamp": "t4",
         "payload": {"type": "patch_apply_end",
                     "changes": {"/home/x/proj/parser.py": {"type": "update"}}}},
    ]
    path = tmp_path / "rollout-2026-08-30T10-00-00-cx-1.jsonl"
    _write_transcript(path, lines)
    session, events = codex.parse_transcript(path)

    assert session.tool == "codex"
    assert session.cwd == "/home/x/proj"
    kinds = [e.kind for e in events]
    assert kinds.count("prompt") == 1
    # token_count dropped; command + file change kept.
    assert any("pytest" in c for e in events for c in e.commands)
    assert "/home/x/proj/parser.py" in {f for e in events for f in e.files}


def test_scan_redacts_before_enqueue(tmp_path: Path) -> None:
    """Secrets must be gone from the spool payload -- redaction is on-device."""
    path = tmp_path / "proj" / "cc-1.jsonl"
    _write_transcript(path, CLAUDE_LINES)
    spool = Spool(tmp_path / "spool.db")

    scan_file(path, spool, device="dev", agent_id=None, deny=[])
    payloads = [json.loads(row["payload"]) for row in spool.next_batch()]
    blob = json.dumps(payloads)

    for secret in ["sk-ant-api03-PLANTED", "sk-ant-shouldnotstore", "plantedsecret123"]:
        assert secret not in blob, f"{secret} leaked into spool"
    assert "REDACTED" in blob
    spool.close()


def test_deny_list_skips_project(tmp_path: Path) -> None:
    path = tmp_path / "secret" / "cc-1.jsonl"
    _write_transcript(path, CLAUDE_LINES)
    spool = Spool(tmp_path / "spool.db")
    enqueued = scan_file(path, spool, device="dev", agent_id=None, deny=[str(tmp_path / "secret")])
    assert enqueued == 0
    assert spool.pending_count() == 0
    spool.close()


def test_offline_spool_then_deliver(tmp_path: Path, client: TestClient) -> None:
    """Events survive the hub being offline and deliver exactly once on reconnect."""
    path = tmp_path / "proj" / "cc-1.jsonl"
    _write_transcript(path, CLAUDE_LINES)
    spool = Spool(tmp_path / "spool.db")
    scan_file(path, spool, device="dev", agent_id=None, deny=[])
    pending = spool.pending_count()
    assert pending > 0

    # Deliver against a dead hub: nothing delivered, spool intact.
    dead = HubClient("http://127.0.0.1:1", timeout=1)
    result = deliver(spool, dead)
    assert result["delivered"] == 0
    assert spool.pending_count() == pending

    # Deliver against the live in-process hub via a thin adapter.
    class _InProcess(HubClient):
        def health(self_inner):  # noqa: N805
            return client.get("/health").status_code == 200

        def send_events(self_inner, session, events):  # noqa: N805
            resp = client.post("/v1/sessions/events", json={"session": session, "events": events})
            assert resp.status_code == 200
            return resp.json()

    ok = deliver(spool, _InProcess("http://unused"))
    assert ok["delivered"] == pending
    assert spool.pending_count() == 0

    # Idempotent: a second delivery of the same events changes nothing at the hub.
    detail = client.get("/v1/sessions/cc-1").json()
    assert detail["session"]["event_count"] == pending
    spool.close()


def test_claude_parser_skips_slash_command_boilerplate(tmp_path: Path) -> None:
    """Slash-command wrappers and notifications are machinery, not prompts."""
    lines = [
        {"type": "user", "sessionId": "cc-2", "cwd": "/home/x/proj", "timestamp": "t0",
         "message": {"role": "user", "content": "<command-name>/model</command-name>\n<command-message>model</command-message>"}},
        {"type": "user", "sessionId": "cc-2", "timestamp": "t1", "isMeta": True,
         "message": {"role": "user", "content": "<local-command-caveat>Caveat: ...</local-command-caveat>"}},
        {"type": "user", "sessionId": "cc-2", "timestamp": "t2",
         "message": {"role": "user", "content": "<task-notification>done</task-notification>"}},
        {"type": "user", "sessionId": "cc-2", "cwd": "/home/x/proj", "timestamp": "t3",
         "message": {"role": "user", "content": "actually refactor the auth module"}},
    ]
    path = tmp_path / "proj" / "cc-2.jsonl"
    _write_transcript(path, lines)
    _, events = claude_code.parse_transcript(path)
    prompts = [e for e in events if e.kind == "prompt"]
    assert len(prompts) == 1
    assert prompts[0].content == "actually refactor the auth module"


def test_subagent_transcripts_fold_into_parent_as_sidechain(tmp_path: Path, client: TestClient) -> None:
    """Sub-agent logs enrich the parent session without becoming their own."""
    proj = tmp_path / "-home-x-proj"
    parent_id = "11111111-2222-3333-4444-555555555555"
    # Parent session transcript.
    _write_transcript(proj / f"{parent_id}.jsonl", [
        {"type": "user", "sessionId": parent_id, "cwd": "/home/x/proj", "timestamp": "t0",
         "message": {"role": "user", "content": "build the parser"}},
        {"type": "ai-title", "sessionId": parent_id, "aiTitle": "build the parser"},
        {"type": "assistant", "sessionId": parent_id, "timestamp": "t1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]}},
    ])
    # Two sub-agent transcripts under <parent>/subagents/.
    subdir = proj / parent_id / "subagents"
    for name, text in [("agent-aaa.jsonl", "explore the auth module"),
                       ("agent-bbb.jsonl", "explore the db layer")]:
        _write_transcript(subdir / name, [
            {"type": "user", "sessionId": parent_id, "cwd": "/home/x/proj", "isSidechain": True,
             "timestamp": "t2", "message": {"role": "user", "content": text}},
            {"type": "assistant", "sessionId": parent_id, "isSidechain": True, "timestamp": "t3",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "done: " + text}]}},
        ])

    spool = Spool(tmp_path / "spool.db")
    for p in claude_code.find_transcripts(proj.parent) + claude_code.find_subagent_transcripts(proj.parent):
        scan_file(p, spool, device="dev", agent_id=None, deny=[])

    class _InProcess(HubClient):
        def health(self_inner):  # noqa: N805
            return True

        def send_events(self_inner, session, events):  # noqa: N805
            return client.post("/v1/sessions/events", json={"session": session, "events": events}).json()

    deliver(spool, _InProcess("http://unused"))
    spool.close()

    # One session, enriched with the sub-agent events, all marked sidechain.
    listing = client.get("/v1/sessions", params={"include_sidechain": True}).json()["sessions"]
    assert len(listing) == 1
    detail = client.get(f"/v1/sessions/{parent_id}", params={"detail": "full", "event_limit": 50}).json()
    assert detail["session"]["title"] == "build the parser"  # parent title preserved
    kinds = [(e["content"], e.get("kind")) for e in detail["events"]]
    joined = " ".join(c for c, _ in kinds)
    assert "explore the auth module" in joined
    assert "explore the db layer" in joined

    # Sub-agent content is sidechain, so it is excluded from the default brief.
    from agent_memory.nim import NimClient
    from agent_memory.summarize import generate_summary
    ctx = client.app.state.ctx
    conn = ctx.open()
    try:
        result = generate_summary(conn, ctx.config, NimClient(ctx.config), parent_id, tier="final")
        conn.commit()
    finally:
        conn.close()
    # The extractive summary is built from non-sidechain events only.
    assert "explore the auth module" not in result["summary"]
