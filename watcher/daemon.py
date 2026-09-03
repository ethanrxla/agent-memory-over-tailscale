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
import os
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_memory.projects import derive_project  # noqa: E402
from agent_memory.redact import path_is_denied, redact_text  # noqa: E402
from watcher import claude_code, codex  # noqa: E402
from watcher.common import ParsedEvent, ParsedSession  # noqa: E402
from watcher.hub_client import HubClient  # noqa: E402
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


def run_cycle(roots: dict[str, list[Path]], spool: Spool, client: HubClient,
              device: str, agent_id: str | None, deny: list[str]) -> dict[str, int]:
    enqueued = 0
    for paths in roots.values():
        for path in paths:
            enqueued += scan_file(path, spool, device, agent_id, deny)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent memory transcript watcher")
    parser.add_argument("--backend-url", default=os.getenv("AGENT_MEMORY_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--shared-key", default=os.getenv("AGENT_MEMORY_SHARED_KEY", ""))
    parser.add_argument("--device", default=platform.node())
    parser.add_argument("--agent-id", default=os.getenv("AGENT_MEMORY_AGENT_ID"))
    parser.add_argument("--spool", default=os.getenv("AGENT_MEMORY_SPOOL", str(DEFAULT_SPOOL)))
    parser.add_argument("--interval", type=float, default=float(os.getenv("AGENT_MEMORY_WATCH_INTERVAL", "20")))
    parser.add_argument("--deny", action="append", default=[], help="Path prefix to skip (repeatable)")
    parser.add_argument("--claude-root", type=Path, default=None)
    parser.add_argument("--codex-root", type=Path, default=None)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--backfill", action="store_true", help="Deliver everything on disk, then exit")
    args = parser.parse_args(argv)

    deny = list(args.deny) + [p for p in os.getenv("AGENT_MEMORY_DENY", "").split(":") if p.strip()]
    spool = Spool(args.spool)
    client = HubClient(args.backend_url, args.shared_key)

    if args.agent_id:
        try:
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
            result = run_cycle(roots, spool, client, args.device, args.agent_id, deny)
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
