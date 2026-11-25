"""Watcher daemon: tail transcripts, redact, spool, deliver.

Pipeline per file:
    read new bytes from the byte-offset checkpoint
    -> parse into the common event stream
    -> derive project_key from the recorded cwd (git-aware)
    -> redact every field on THIS device, before anything is queued
    -> enqueue into the local spool (survives the hub being offline)
    -> drain the spool to the hub in idempotent batches

Redaction happening here, on the source device, is the guarantee that raw
secrets never cross the network.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_memory.projects import derive_project  # noqa: E402
from agent_memory.redact import path_is_denied, redact_text  # noqa: E402
from watcher import claude_code, codex, hermes, history  # noqa: E402
from watcher.common import ParsedEvent, ParsedSession  # noqa: E402
from watcher.hub_client import HubClient, ResolvingHubClient  # noqa: E402
from watcher.spool import Spool  # noqa: E402

DEFAULT_SPOOL = Path.home() / ".cache" / "agent-memory" / "spool.db"


def _redact_event(event: ParsedEvent) -> dict[str, Any]:
    data = asdict(event)
    data["content"] = redact_text(event.content)
    data["commands"] = [redact_text(c) for c in event.commands]
    # File paths are structural, not secret; keep them as-is for retrieval.
    return data


def _session_payload(session: ParsedSession, device: str, agent_id: str | None) -> dict[str, Any]:
    project = derive_project(session.cwd)
    return {
        "session_id": session.session_id,
        "project_key": project["project_key"],
        "display_name": project["display_name"],
        "repo_root": project["repo_root"],
        "git_remote": project["git_remote"],
        "agent_id": agent_id,
        "device_name": device,
        "tool": session.tool,
        "cwd": session.cwd,
        "git_branch": session.git_branch,
        "ai_title": session.ai_title,
        # Match SessionUpsertRequest's bound so one unusually long operator
        # message cannot block delivery of the entire durable spool.
        "last_prompt": redact_text(session.last_prompt)[:4000] if session.last_prompt else None,
        "is_sidechain": session.is_sidechain,
        "started_at": session.started_at,
    }


def _is_subagent(path: Path) -> bool:
    return path.parent.name == "subagents"


def _subagent_context(path: Path) -> tuple[str, int]:
    """Return (parent_session_id, seq_base) for a sub-agent transcript.

    The parent session id is the directory that owns the subagents folder. The
    seq base spaces each sub-agent's events into their own range so they never
    collide with the parent's events (base 0) or with each other on the
    (session_id, seq) idempotency key. The base is derived from the file's
    sorted position among its siblings, so re-runs are stable.
    """
    parent_id = path.parent.parent.name
    siblings = sorted(p.name for p in path.parent.glob("*.jsonl"))
    ordinal = siblings.index(path.name) if path.name in siblings else 0
    return parent_id, (ordinal + 1) * 1_000_000


def scan_file(path: Path, spool: Spool, device: str, agent_id: str | None, deny: list[str]) -> int:
    """Parse a transcript and enqueue any events past the checkpoint.

    Transcripts are append-mostly. We re-parse the whole file (cheap after
    filtering) but only enqueue events with seq beyond last_seq, and only when
    the parsed content actually grew, tracked by byte offset.
    """
    path_str = str(path)
    if path_is_denied(path_str, deny):
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0

    byte_offset, last_seq = spool.checkpoint(path_str)
    if size <= byte_offset and last_seq >= 0:
        return 0  # nothing appended since last scan

    parser = codex if path.name.startswith("rollout-") else claude_code
    session, events = parser.parse_transcript(path)
    if session is None:
        return 0

    if _is_subagent(path):
        # Fold sub-agent work into the parent session as sidechain events:
        # searchable, but excluded from summaries and the graph.
        parent_id, seq_base = _subagent_context(path)
        session.session_id = parent_id
        session.is_sidechain = False  # the parent session is not itself sidechain
        session.ai_title = None       # never overwrite the parent's title/prompt
        session.last_prompt = None
        for event in events:
            event.is_sidechain = True
            event.seq += seq_base

    session_payload = _session_payload(session, device, agent_id)
    session_id = session_payload["session_id"]
    enqueued = 0
    max_seq = last_seq
    for event in events:
        if event.seq <= last_seq:
            continue
        payload = {"session": session_payload, "event": _redact_event(event)}
        if spool.enqueue(session_id, event.seq, payload):
            enqueued += 1
        max_seq = max(max_seq, event.seq)

    spool.set_checkpoint(path_str, size, max_seq)
    spool.commit()
    return enqueued


def scan_hermes(db_path: Path | None, spool: Spool, device: str, agent_id: str | None,
                deny: list[str]) -> int:
    """Enqueue new messages from the Hermes Agent session store (state.db).

    Checkpoints are keyed ``hermes:<session id>`` with the highest message row
    id seen as the offset, so only sessions that gained messages are re-read.
    """
    if db_path is None:
        return 0
    conn = hermes.connect(db_path)
    if conn is None:
        return 0
    enqueued = 0
    try:
        for row in hermes.list_sessions(conn):
            key = f"hermes:{row['id']}"
            top = hermes.max_message_id(conn, row["id"])
            byte_offset, last_seq = spool.checkpoint(key)
            if top <= byte_offset and last_seq >= 0:
                continue
            session, events = hermes.parse_session(conn, row, fallback_cwd=str(Path.home()))
            if session.cwd and path_is_denied(session.cwd, deny):
                spool.set_checkpoint(key, top, max(last_seq, 0))
                continue
            payload = _session_payload(session, device, agent_id)
            max_seq = last_seq
            for event in events:
                if event.seq <= last_seq:
                    continue
                body = {"session": payload, "event": _redact_event(event)}
                if spool.enqueue(payload["session_id"], event.seq, body):
                    enqueued += 1
                max_seq = max(max_seq, event.seq)
            spool.set_checkpoint(key, top, max_seq)
            spool.commit()
    except sqlite3.Error as exc:  # Hermes holds the db open; retry next cycle
        print(f"[watcher] hermes scan deferred: {exc}", file=sys.stderr)
    finally:
        conn.close()
    return enqueued


def deliver(spool: Spool, client: HubClient, batch_size: int = 200) -> dict[str, int]:
    """Drain the spool to the hub, grouping events by session per request."""
    stats = {"delivered": 0, "batches": 0, "failed": 0}
    if not client.health():
        stats["failed"] = spool.pending_count()
        return stats

    while True:
        rows = spool.next_batch(batch_size)
        if not rows:
            break
        grouped: dict[str, dict[str, Any]] = {}
        row_ids: list[int] = []
        for row in rows:
            import json as _json

            payload = _json.loads(row["payload"])
            last_prompt = payload["session"].get("last_prompt")
            if isinstance(last_prompt, str):
                payload["session"]["last_prompt"] = last_prompt[:4000]
            session_id = row["session_id"]
            bucket = grouped.setdefault(session_id, {"session": payload["session"], "events": []})
            bucket["events"].append(payload["event"])
            row_ids.append(row["id"])

        try:
            for bucket in grouped.values():
                client.send_events(bucket["session"], bucket["events"])
        except Exception:  # noqa: BLE001 - any delivery error: keep the spool, retry later
            stats["failed"] += len(row_ids)
            break

        spool.mark_delivered(row_ids)
        stats["delivered"] += len(row_ids)
        stats["batches"] += 1

    spool.prune_delivered()
    return stats


def ingest_history(spool: Spool, roots: dict[str, list[Path]]) -> int:
    """Recover orphaned sessions (transcript gone, prompts survive in history).

    Prompt-only and lower fidelity, so marked source=history. Skips any session
    that has a transcript, so nothing already ingested is duplicated.
    """
    disk_ids: set[str] = set()
    for paths in roots.values():
        for path in paths:
            if path.parent.name == "subagents":
                disk_ids.add(path.parent.parent.name)
            elif path.name.startswith("rollout-"):
                sess, _ = codex.parse_transcript(path)
                disk_ids.add(sess.session_id)
            else:
                disk_ids.add(path.stem)

    enqueued = 0
    for session, events in history.orphan_sessions(disk_ids):
        project = derive_project(session.cwd)
        payload = {
            "session_id": session.session_id,
            "project_key": project["project_key"],
            "display_name": project["display_name"],
            "repo_root": project["repo_root"],
            "git_remote": project["git_remote"],
            "tool": session.tool,
            "cwd": session.cwd,
            "ai_title": redact_text(session.ai_title) if session.ai_title else None,
            "last_prompt": redact_text(session.last_prompt) if session.last_prompt else None,
            "started_at": session.started_at,
            "metadata": {"source": "history", "fidelity": "prompts_only"},
        }
        for event in events:
            body = {"session": payload, "event": _redact_event(event)}
            if spool.enqueue(session.session_id, event.seq, body):
                enqueued += 1
    spool.commit()
    return enqueued


def run_cycle(roots: dict[str, list[Path]], spool: Spool, client: HubClient,
              device: str, agent_id: str | None, deny: list[str],
              hermes_db: Path | None = None) -> dict[str, int]:
    enqueued = 0
    for paths in roots.values():
        for path in paths:
            enqueued += scan_file(path, spool, device, agent_id, deny)
    enqueued += scan_hermes(hermes_db, spool, device, agent_id, deny)
    result = deliver(spool, client)
    result["enqueued"] = enqueued
    result["pending"] = spool.pending_count()
    return result


def discover(claude_root: Path | None, codex_root: Path | None) -> dict[str, list[Path]]:
    roots: dict[str, list[Path]] = {}
    if claude_root:
        roots["claude"] = claude_code.find_transcripts(claude_root)
        roots["claude_subagents"] = claude_code.find_subagent_transcripts(claude_root)
    else:
        roots["claude"] = claude_code.find_transcripts()
        roots["claude_subagents"] = claude_code.find_subagent_transcripts()
    roots["codex"] = codex.find_transcripts(codex_root) if codex_root else codex.find_transcripts()
    return roots


def build_client(backend_url: str | None, shared_key: str, no_fallback: bool = False) -> HubClient:
    """A client that delivers to the first healthy configured hub.

    Candidates, in order: --backend-url / AGENT_MEMORY_URL, then ``hubs`` from
    ~/.agent-memory/config.json, then DEFAULT_HUBS (empty as shipped) -- the
    same list memory.py and the MCP server use. --no-fallback pins the first one.
    """
    from agent_memory.hubs import load_resolver

    resolver = load_resolver()
    if resolver is None:  # skill file missing: behave exactly like before
        return HubClient(backend_url or "http://127.0.0.1:8787", shared_key)
    env = dict(os.environ)
    if backend_url:
        env["AGENT_MEMORY_URL"] = backend_url
    candidates = resolver.candidate_hubs(resolver.load_config(), env)
    if no_fallback:
        candidates = candidates[:1]
    key = shared_key or resolver.shared_key(resolver.load_config())
    return ResolvingHubClient(candidates, key, resolve=resolver.resolve_hub)


def write_state(spool: Spool, client: HubClient, result: dict[str, int], state: dict[str, Any]) -> None:
    """Record the last cycle next to the spool so `memory.py status` can show it."""
    selected = getattr(client, "selected", client.base_url)
    primary = getattr(client, "primary", client.base_url)
    fallback = state.setdefault("fallback_deliveries", {})
    if selected and selected != primary and result.get("delivered"):
        fallback[selected] = fallback.get(selected, 0) + result["delivered"]
    state.update(
        pid=os.getpid(),
        last_cycle_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        hub=selected,
        preferred_hub=primary,
        candidates=[url for url, _ in getattr(client, "candidates", [(client.base_url, "flag")])],
        delivered=result.get("delivered", 0),
        pending=result.get("pending", 0),
        failed=result.get("failed", 0),
    )
    path = spool.path.parent / "watcher-state.json"
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[watcher] could not write {path}: {exc}", file=sys.stderr)


def _load_state(spool: Spool) -> dict[str, Any]:
    try:
        data = json.loads((spool.path.parent / "watcher-state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent memory transcript watcher")
    parser.add_argument("--backend-url", default=os.getenv("AGENT_MEMORY_URL"),
                        help="Preferred hub; other configured hubs are used while it is down")
    parser.add_argument("--no-fallback", action="store_true",
                        help="Only ever deliver to --backend-url (spool while it is down)")
    parser.add_argument("--shared-key", default=os.getenv("AGENT_MEMORY_SHARED_KEY", ""))
    parser.add_argument("--device", default=platform.node())
    parser.add_argument("--agent-id", default=os.getenv("AGENT_MEMORY_AGENT_ID"))
    parser.add_argument("--spool", default=os.getenv("AGENT_MEMORY_SPOOL", str(DEFAULT_SPOOL)))
    parser.add_argument("--interval", type=float, default=float(os.getenv("AGENT_MEMORY_WATCH_INTERVAL", "20")))
    parser.add_argument("--deny", action="append", default=[], help="Path prefix to skip (repeatable)")
    parser.add_argument("--claude-root", type=Path, default=None)
    parser.add_argument("--codex-root", type=Path, default=None)
    parser.add_argument("--hermes-db", type=Path, default=None,
                        help="Hermes Agent state.db (default: auto-detect)")
    parser.add_argument("--no-hermes", action="store_true", help="Skip Hermes Agent sessions")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--backfill", action="store_true", help="Deliver everything on disk, then exit")
    parser.add_argument("--no-history", action="store_true",
                        help="Skip recovering orphaned sessions from prompt history during backfill")
    args = parser.parse_args(argv)

    deny = list(args.deny) + [p for p in os.getenv("AGENT_MEMORY_DENY", "").split(os.pathsep) if p.strip()]
    hermes_db = None if args.no_hermes else (args.hermes_db or hermes.default_db())
    spool = Spool(args.spool)
    client = build_client(args.backend_url, args.shared_key, args.no_fallback)
    state = _load_state(spool)

    if args.agent_id:
        try:
            client.health()  # point a resolving client at a live hub first
            client.register_agent({
                "agent_id": args.agent_id,
                "display_name": args.agent_id,
                "device_name": args.device,
                "capabilities": ["watcher"],
            })
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] agent registration deferred: {exc}", file=sys.stderr)

    try:
        while True:
            roots = discover(args.claude_root, args.codex_root)
            total = sum(len(v) for v in roots.values())
            # On backfill, also recover orphaned sessions from prompt history.
            if args.backfill and not args.no_history:
                hist = ingest_history(spool, roots)
                if hist:
                    print(f"[watcher] recovered {hist} prompt events from orphaned history sessions", flush=True)
            result = run_cycle(roots, spool, client, args.device, args.agent_id, deny, hermes_db)
            write_state(spool, client, result, state)
            print(
                f"[watcher] files={total} enqueued={result['enqueued']} "
                f"delivered={result['delivered']} pending={result['pending']} failed={result['failed']}",
                flush=True,
            )
            if args.once or args.backfill:
                # Backfill keeps draining until the spool is empty or delivery stalls.
                if args.backfill and result["pending"] and result["failed"] == 0:
                    continue
                break
            time.sleep(args.interval)
    finally:
        spool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
