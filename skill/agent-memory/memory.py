#!/usr/bin/env python3
"""Self-contained client for the Agent Memory hub.

Bundled inside the skill so the whole folder can be dropped into any machine's
skills directory and work with no repo checkout and no MCP server -- it only
needs AGENT_MEMORY_URL to point at the hub. Stdlib only.

Usage:
  memory.py brief                       # project-scoped session-start brief
  memory.py search "why did we drop X"  # semantic + keyword search, this project
  memory.py sessions                    # recent sessions in this project
  memory.py threads                     # open threads in this project
  memory.py decide "Title" "Body" [--rationale "..."]
  memory.py global-search "query"       # search across ALL projects

Scope comes from the current directory's git remote (or path); pass --cwd to
override, --scope project|linked|global to widen.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("AGENT_MEMORY_URL", "http://127.0.0.1:8787").rstrip("/")
KEY = os.getenv("AGENT_MEMORY_SHARED_KEY", "")

_URL = re.compile(r"^[a-zA-Z][\w+.-]*://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$")
_SSH = re.compile(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)[:/](.+?)(?:\.git)?/?$")


def _run(args: list[str]) -> str | None:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def project_key(cwd: str) -> str:
    root = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if root:
        remote = _run(["git", "-C", cwd, "remote", "get-url", "origin"])
        if remote:
            m = _URL.match(remote) or _SSH.match(remote)
            if m:
                return f"{m.group(1).lower()}/{m.group(2).strip('/').lower()}"
        return f"path:{os.path.realpath(root)}"
    return f"path:{os.path.realpath(cwd)}"


def branch(cwd: str) -> str | None:
    return _run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"])


def request(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if KEY:
        req.add_header("X-Shared-Key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        sys.exit(f"hub error HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"cannot reach hub at {BASE} ({exc}). Set AGENT_MEMORY_URL.")


def _q(pk: str) -> str:
    return urllib.parse.quote(pk, safe="")


# Directory names carry scaffolding words that bury the distinctive part of the
# name: "RedLamb-build" scores 0.20 against the sessions that built RedLamb,
# while "RedLamb" scores 1.00. Strip these before seeding a search.
_GENERIC_DIR_TOKENS = {
    "bin", "build", "builds", "code", "debug", "dist", "final", "master", "new",
    "obj", "old", "out", "output", "project", "projects", "release", "repo",
    "repos", "source", "sources", "src", "target", "temp", "test", "tests",
    "tmp", "workspace",
}

# Below this, a hit is noise. A forced weak match is what sends an agent off
# track, so an empty hint is better than a confident wrong one.
_NEARBY_MIN_SCORE = 0.4


def _search_seed(cwd: str) -> str:
    """Turn a directory name into a query the hub can actually match."""
    base = os.path.basename(os.path.realpath(cwd))
    parts = [p for p in re.split(r"[-_.\s]+", base) if p]
    kept = [p for p in parts if p.lower() not in _GENERIC_DIR_TOKENS]
    return " ".join(kept or parts)


def _lexical_nearby(projects: list[dict], pk: str, cwd: str) -> list[str]:
    """Neighbours whose key or repo root overlaps this path.

    Catches the common local case -- a git subdirectory and its loose parent
    are distinct project_keys even though they are the same work.
    """
    base = os.path.basename(os.path.realpath(cwd)).lower()
    path_frag = os.path.realpath(cwd).lower()
    hits = []
    for pr in projects:
        if pr["project_key"] == pk or not pr.get("session_count"):
            continue
        key = pr["project_key"].lower()
        root = (pr.get("repo_root") or "").lower()
        name_match = bool(base) and (base in key or base in root)
        path_match = bool(root) and (root.startswith(path_frag) or path_frag.startswith(root))
        if name_match or path_match:
            hits.append(pr["project_key"])
    return hits


def _semantic_nearby(pk: str, cwd: str) -> dict[str, float]:
    """Neighbours that share no path or name overlap at all.

    The same project worked on from another machine lands under an unrelated
    key -- RedLamb built on Windows lives under path:C:\\Users\\theif, which
    has no substring in common with a /Volumes/.../RedLamb-build checkout.
    Only meaning connects them, so ask the hub by meaning.
    """
    seed = _search_seed(cwd)
    if not seed:
        return {}
    try:
        d = request("POST", "/v1/search", {
            "query": seed, "project_key": None, "scope": "global",
            "limit": 12, "min_score": _NEARBY_MIN_SCORE,
        })
    except SystemExit:
        return {}
    best: dict[str, float] = {}
    for r in d.get("results") or []:
        key, score = r["project_key"], r.get("relevance", 0.0)
        if key == pk or score < _NEARBY_MIN_SCORE:
            continue
        best[key] = max(best.get(key, 0.0), score)
    return best


def _suggest_nearby(pk: str, cwd: str) -> None:
    """When a project has no sessions, point at related projects.

    project_key is derived from git remote or path, so the same work reached
    from another checkout, another parent directory, or another machine is a
    different project. This surfaces the neighbours so the agent is never left
    at a dead end -- which is worse than no memory at all, because it reads as
    "this is new work" and the agent starts inventing.
    """
    try:
        projects = request("GET", "/v1/projects?limit=200")["projects"]
    except SystemExit:
        return
    counts = {p["project_key"]: p.get("session_count", 0) for p in projects}

    lexical = _lexical_nearby(projects, pk, cwd)
    semantic = _semantic_nearby(pk, cwd)

    ordered: list[tuple[str, str]] = []
    for key in sorted(lexical, key=lambda k: -counts.get(k, 0)):
        ordered.append((key, "name/path match"))
    for key, score in sorted(semantic.items(), key=lambda kv: -kv[1]):
        if key in lexical or not counts.get(key):
            continue
        ordered.append((key, f"semantic match {score:.2f}"))
    if not ordered:
        return

    print("\nRelated projects with recorded sessions:")
    for key, why in ordered[:6]:
        print(f"  - {key}  ({counts.get(key, 0)} sessions, {why})")
    print('Re-run with --project-key <key>, or use: memory.py global-search "<query>"')


def cmd_brief(a):
    pk = a.project_key or project_key(a.cwd)
    params = {"max_sessions": str(a.max_sessions)}
    br = branch(a.cwd)
    if br:
        params["branch"] = br
    d = request("GET", f"/v1/projects/{_q(pk)}/brief?{urllib.parse.urlencode(params)}")
    if d.get("message") and not d["sessions"]:
        print(f"[{d['display_name']}] {d['message']}")
        # Current hubs embed the hint in the message and return it structured.
        # Only an older hub leaves the client to work it out.
        if not d.get("related_projects"):
            _suggest_nearby(pk, a.cwd)
        return
    print(f"# Project brief: {d['display_name']}  ({d['session_count']} recent sessions, ~{d['estimated_tokens']} tokens)")
    for s in d["sessions"]:
        print(f"\n## {s['title'] or s['session_id'][:8]}  ({s['tool']}, {s['status']})")
        print(s["summary"])
    if d["decisions"]:
        print("\n## Decisions in effect")
        for x in d["decisions"]:
            print(f"  - {x}")
    if d["open_threads"]:
        print("\n## Open threads")
        for x in d["open_threads"]:
            print(f"  - {x}")


def _print_results(d):
    if not d["results"]:
        print(d.get("message") or "No relevant prior context.")
        return
    for r in d["results"]:
        tag = r["source_type"]
        print(f"\n● {r['title']}  [{tag} · {r['project_key']} · relevance {r['relevance']:.2f}]")
        print("  " + r["content"].strip().replace("\n", "\n  ")[:600])


def cmd_search(a):
    pk = None if a.scope == "global" else (a.project_key or project_key(a.cwd))
    body = {"query": a.query, "project_key": pk, "scope": a.scope, "limit": a.limit}
    if a.min_score is not None:
        body["min_score"] = a.min_score
    _print_results(request("POST", "/v1/search", body))


def cmd_global_search(a):
    a.scope = "global"
    cmd_search(a)


def cmd_sessions(a):
    pk = a.project_key or project_key(a.cwd)
    d = request("GET", f"/v1/sessions?{urllib.parse.urlencode({'project_key': pk, 'limit': a.limit})}")
    for s in d["sessions"]:
        print(f"{s['last_event_at'][:16]}  {s['status']:6}  {s['tool']:11}  {s['title'] or s['session_id'][:8]}  ({s['event_count']} events)")
    if not d["sessions"]:
        print("No sessions recorded for this project yet.")


def cmd_threads(a):
    pk = a.project_key or project_key(a.cwd)
    d = request("GET", f"/v1/projects/{_q(pk)}/threads")
    if not d["open_threads"]:
        print("No open threads.")
    for t in d["open_threads"]:
        print(f"  - {t['thread']}  (from: {t['session_title'] or t['session_id'][:8]})")


def cmd_decide(a):
    pk = a.project_key or project_key(a.cwd)
    agent = os.getenv("AGENT_MEMORY_AGENT_ID") or platform.node() + "-agent"
    body = {"agent_id": agent, "project_key": pk, "cwd": a.cwd,
            "title": a.title, "content": a.content, "rationale": a.rationale, "tags": []}
    # The hub requires the agent be registered before writing.
    request("POST", "/v1/agents/register", {"agent_id": agent, "display_name": agent, "capabilities": ["skill"]})
    r = request("POST", "/v1/decisions", body)
    print(f"recorded decision '{a.title}' -> {r['entry_id']}")


def cmd_context(a):
    """One-shot for the /memory slash command: brief with no query, else search."""
    if getattr(a, "query", "") and a.query.strip():
        a.limit = 6
        a.min_score = None
        cmd_search(a)
    else:
        a.max_sessions = 5
        cmd_brief(a)


def main() -> int:
    p = argparse.ArgumentParser(prog="memory", description="Agent Memory hub client")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--project-key")
    p.add_argument("--scope", choices=["project", "linked", "global"], default="project")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("brief"); b.add_argument("--max-sessions", type=int, default=5); b.set_defaults(fn=cmd_brief)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--limit", type=int, default=6)
    s.add_argument("--min-score", type=float); s.set_defaults(fn=cmd_search)
    g = sub.add_parser("global-search"); g.add_argument("query"); g.add_argument("--limit", type=int, default=6)
    g.add_argument("--min-score", type=float); g.set_defaults(fn=cmd_global_search)
    ss = sub.add_parser("sessions"); ss.add_argument("--limit", type=int, default=15); ss.set_defaults(fn=cmd_sessions)
    th = sub.add_parser("threads"); th.set_defaults(fn=cmd_threads)
    de = sub.add_parser("decide"); de.add_argument("title"); de.add_argument("content")
    de.add_argument("--rationale"); de.set_defaults(fn=cmd_decide)
    cx = sub.add_parser("context"); cx.add_argument("query", nargs="?", default="")
    cx.set_defaults(fn=cmd_context)

    a = p.parse_args()
    a.fn(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
