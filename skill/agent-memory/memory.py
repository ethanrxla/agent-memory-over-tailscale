#!/usr/bin/env python3
"""Self-contained client for the Agent Memory hub.

Bundled inside the skill so the whole folder can be dropped into any machine's
skills directory and work with no repo checkout and no MCP server. Stdlib only.

Usage:
  memory.py status                      # which hub is reachable, watcher + spool state
  memory.py brief                       # project-scoped session-start brief
  memory.py search "why did we drop X"  # semantic + keyword search, this project
  memory.py sessions                    # recent sessions in this project
  memory.py threads                     # open threads in this project
  memory.py decide "Title" "Body" [--rationale "..."]
  memory.py global-search "query"       # search across ALL projects
  memory.py sync --from URL --to URL    # copy sessions/entries hub -> hub (idempotent)

Hub resolution: there is no hard-coded hub. Candidates come, in order, from
AGENT_MEMORY_URL (comma-separated allowed), then ``hubs`` in
~/.agent-memory/config.json, then DEFAULT_HUBS (empty as shipped). Each is
probed with GET /health (short timeout) and the first healthy one is used; the
choice is cached briefly. If none answers, the command fails with the list of hubs tried
-- ask the user which hub to use, or pass --start-local on a machine that has
the hub code to start one bound to this device's Tailscale address.

Scope comes from the current directory's git remote (or path); pass --cwd to
override, --scope project|linked|global to widen.

This module is also imported (by path) by the watcher and the MCP server so all
three clients resolve hubs the same way; keep it import-safe and stdlib-only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# No hubs are built in: each install points at its own hub(s) through
# AGENT_MEMORY_URL or ``hubs`` in ~/.agent-memory/config.json. Add URLs here only
# in a private copy, probed after those two (``"include_defaults": false`` in
# config.json skips them).
DEFAULT_HUBS: tuple[str, ...] = ()
DEFAULT_PORT = 8787
EXIT_NO_HUB = 3


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def state_dir() -> Path:
    return Path(os.getenv("AGENT_MEMORY_STATE_DIR") or (Path.home() / ".agent-memory")).expanduser()


def config_path() -> Path:
    return Path(os.getenv("AGENT_MEMORY_CONFIG") or (state_dir() / "config.json")).expanduser()


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read the optional JSON config. Never raises; problems land in ``_error``."""
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        return {"_error": f"{path}: {exc}"}
    if not isinstance(data, dict):
        return {"_error": f"{path}: top level must be a JSON object"}
    return data


def shared_key(cfg: dict[str, Any] | None = None) -> str:
    """The shared key comes only from the AGENT_MEMORY_SHARED_KEY (user) env var.

    Deliberately never read from config.json: keys do not belong in files.
    ``cfg`` is accepted for call-site symmetry and ignored.
    """
    return os.getenv("AGENT_MEMORY_SHARED_KEY", "")


def _norm(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def candidate_hubs(cfg: dict[str, Any] | None = None,
                   env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Ordered, de-duplicated (url, source) pairs to probe."""
    env = os.environ if env is None else env
    cfg = load_config() if cfg is None else cfg
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: Any, source: str) -> None:
        if not isinstance(url, str):
            return
        u = _norm(url)
        if u and u not in seen:
            seen.add(u)
            out.append((u, source))

    for part in (env.get("AGENT_MEMORY_URL") or "").split(","):
        add(part, "env AGENT_MEMORY_URL")
    hubs = cfg.get("hubs") or []
    if isinstance(hubs, str):
        hubs = [hubs]
    for url in hubs:
        add(url, "config")
    if cfg.get("include_defaults", True):
        for url in DEFAULT_HUBS:
            add(url, "default")
    return out


# --------------------------------------------------------------------------
# Probing and resolution
# --------------------------------------------------------------------------

def probe_timeout() -> float:
    try:
        return float(os.getenv("AGENT_MEMORY_PROBE_TIMEOUT", "2.5"))
    except ValueError:
        return 2.5


def probe_hub(url: str, key: str = "", timeout: float | None = None) -> dict[str, Any]:
    """GET <url>/health. Returns {url, ok, error, ms}; never raises."""
    timeout = probe_timeout() if timeout is None else timeout
    started = time.monotonic()
    req = urllib.request.Request(url.rstrip("/") + "/health", method="GET")
    if key:
        req.add_header("X-Shared-Key", key)
    result: dict[str, Any] = {"url": url, "ok": False, "error": None, "ms": None}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        if isinstance(body, dict) and body.get("status") == "ok":
            result["ok"] = True
        else:
            result["error"] = "unexpected /health response"
    except urllib.error.HTTPError as exc:
        hint = " (shared key missing or rejected)" if exc.code in (401, 403) else ""
        result["error"] = f"HTTP {exc.code}{hint}"
    except (TimeoutError, OSError, urllib.error.URLError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason) or exc.__class__.__name__
        if "timed out" in text.lower() or isinstance(reason, TimeoutError):
            text = f"timed out after {timeout:g}s"
        result["error"] = text
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


def _cache_file() -> Path:
    return state_dir() / "hub-cache.json"


def cache_ttl() -> float:
    try:
        return float(os.getenv("AGENT_MEMORY_HUB_CACHE_TTL", "120"))
    except ValueError:
        return 120.0


def read_cache() -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("url") else None


def write_cache(url: str) -> None:
    try:
        _cache_file().parent.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps({"url": url, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def clear_cache() -> None:
    try:
        _cache_file().unlink()
    except OSError:
        pass


def resolve_hub(candidates: list[tuple[str, str]] | None = None, *, key: str | None = None,
                timeout: float | None = None, use_cache: bool = True,
                probe: Callable[..., dict[str, Any]] | None = None,
                now: Callable[[], float] | None = None) -> dict[str, Any]:
    """Pick the first healthy hub in candidate order.

    Candidates are probed in parallel (so dead hubs cost one timeout, not one
    each) but the winner is the first healthy one in *configured* order. The
    result is cached for AGENT_MEMORY_HUB_CACHE_TTL seconds.
    """
    probe = probe or probe_hub  # looked up at call time so it can be patched
    now = now or time.time
    cfg = load_config()
    candidates = candidate_hubs(cfg) if candidates is None else candidates
    key = shared_key(cfg) if key is None else key
    urls = [u for u, _ in candidates]
    result: dict[str, Any] = {"selected": None, "tried": [], "cached": False,
                              "candidates": candidates}
    if use_cache:
        cached = read_cache()
        if cached and cached["url"] in urls and now() - float(cached.get("at", 0)) < cache_ttl():
            result.update(selected=cached["url"], cached=True)
            return result
    if not urls:
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        tried = list(pool.map(lambda u: probe(u, key, timeout), urls))
    for (url, source), probe_result in zip(candidates, tried):
        probe_result.setdefault("source", source)
    result["tried"] = tried
    for probe_result in tried:
        if probe_result.get("ok"):
            result["selected"] = probe_result["url"]
            if use_cache:
                write_cache(probe_result["url"])
            break
    return result


def format_tried(tried: list[dict[str, Any]]) -> str:
    lines = []
    for i, t in enumerate(tried, 1):
        state = f"OK ({t.get('ms')} ms)" if t.get("ok") else f"unreachable: {t.get('error')}"
        lines.append(f"  {i}. {t['url']}  [{t.get('source', '?')}]  {state}")
    return "\n".join(lines) or ("  (none configured: set AGENT_MEMORY_URL, or \"hubs\" in "
                                 f"{config_path()})")


def no_hub_message(tried: list[dict[str, Any]], local_code: Path | None) -> str:
    msg = ["No agent-memory hub is reachable. Hubs tried, in order:", format_tried(tried), ""]
    if local_code:
        msg.append(
            f"This machine has the hub code ({local_code}). Re-run with --start-local "
            "to start a hub bound to this device's Tailscale address, or ASK THE USER "
            "which hub to use."
        )
    else:
        msg.append("ASK THE USER which hub to use. Do not guess and do not drop the work:")
    msg.append(
        "Point at a hub with --hub URL, AGENT_MEMORY_URL, or `hubs` in "
        f"{config_path()}. The watcher keeps spooling sessions until a hub is reachable."
    )
    return "\n".join(msg)


# --------------------------------------------------------------------------
# Starting a local hub (only on machines that have the hub code)
# --------------------------------------------------------------------------

def _is_hub_repo(path: Path) -> bool:
    return (path / "main.py").is_file() and (path / "agent_memory" / "app.py").is_file()


def find_local_hub_code(cfg: dict[str, Any] | None = None) -> Path | None:
    """The hub repo (or an installed agent_memory package) on this machine, if any."""
    cfg = load_config() if cfg is None else cfg
    local = cfg.get("local_hub") if isinstance(cfg.get("local_hub"), dict) else {}
    options = [local.get("repo"), os.getenv("AGENT_MEMORY_REPO")]
    here = Path(__file__).resolve()
    options.append(str(here.parents[2]) if len(here.parents) > 2 else None)
    for opt in options:
        if opt and _is_hub_repo(Path(opt).expanduser()):
            return Path(opt).expanduser().resolve()
    try:
        import importlib.util
        spec = importlib.util.find_spec("agent_memory")
    except (ImportError, ValueError):
        spec = None
    if spec and spec.origin:
        return Path(spec.origin).resolve().parent.parent
    return None


def tailscale_address() -> dict[str, str | None]:
    """This device's tailnet IPv4 and MagicDNS name (None when unavailable)."""
    exe = shutil.which("tailscale") or shutil.which("tailscale.exe")
    out: dict[str, str | None] = {"ip": None, "dns": None}
    if not exe:
        return out
    ip = _run([exe, "ip", "-4"])
    if ip:
        out["ip"] = ip.splitlines()[0].strip()
    raw = _run([exe, "status", "--self", "--json"])
    if raw:
        try:
            out["dns"] = (json.loads(raw).get("Self", {}).get("DNSName") or "").rstrip(".") or None
        except ValueError:
            pass
    return out


def _hub_python(repo: Path, cfg_local: dict[str, Any]) -> str:
    if cfg_local.get("python"):
        return str(cfg_local["python"])
    for venv in (".venv", "venv", ".venv-recovery"):
        for rel in ("Scripts/python.exe", "bin/python"):
            cand = repo / venv / rel
            if cand.is_file():
                return str(cand)
    return sys.executable


def start_local_hub(cfg: dict[str, Any] | None = None, *, port: int | None = None,
                    wait: float = 30.0, launcher: Callable[..., Any] | None = None,
                    probe: Callable[..., dict[str, Any]] | None = None,
                    address: Callable[[], dict[str, str | None]] | None = None,
                    sleep: Callable[[float], None] | None = None) -> str:
    """Start a hub from the local code, bound to this device's Tailscale address.

    Returns the hub URL once /health answers. Raises RuntimeError otherwise.
    """
    launcher = launcher or subprocess.Popen
    probe = probe or probe_hub
    address = address or tailscale_address
    sleep = sleep or time.sleep
    cfg = load_config() if cfg is None else cfg
    local = cfg.get("local_hub") if isinstance(cfg.get("local_hub"), dict) else {}
    repo = find_local_hub_code(cfg)
    if repo is None:
        raise RuntimeError("no hub code on this machine (repo or agent_memory package)")
    addr = address()
    if not addr.get("ip"):
        raise RuntimeError("Tailscale is not running here (`tailscale ip -4` failed); "
                           "refusing to start a hub that other devices cannot reach")
    port = int(port or local.get("port") or DEFAULT_PORT)
    python = _hub_python(repo, local)
    script = repo / "scripts" / "start_local_hub.py"
    if script.is_file():
        cmd = [python, str(script), "--host", str(addr["ip"]), "--port", str(port)]
        if local.get("db"):
            cmd += ["--db", str(local["db"])]
    elif _is_hub_repo(repo):
        cmd = [python, "-m", "uvicorn", "main:app", "--host", str(addr["ip"]), "--port", str(port)]
    else:  # pip-installed package: no main.py, use the app factory
        cmd = [python, "-m", "uvicorn", "--factory", "agent_memory.app:create_app",
               "--host", str(addr["ip"]), "--port", str(port)]
    log_path = state_dir() / "local-hub.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"cwd": str(repo), "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as log:
        launcher(cmd, stdout=log, stderr=log, **kwargs)
    url = f"http://{addr.get('dns') or addr['ip']}:{port}"
    key = shared_key(cfg)
    deadline = time.monotonic() + wait
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = probe(url, key, 2.0)
        if last.get("ok"):
            write_cache(url)
            return url
        sleep(1.0)
    raise RuntimeError(f"started a local hub but {url}/health never answered "
                       f"({last.get('error')}); see {log_path}")


# --------------------------------------------------------------------------
# Hub selection for this process
# --------------------------------------------------------------------------

_OPTS: dict[str, Any] = {"hub": None, "start_local": False, "use_cache": True}
_SELECTED: dict[str, Any] = {"url": None}


def select_hub(*, force_refresh: bool = False) -> str:
    """Resolve (once per process) the hub to talk to, or exit with guidance."""
    if _OPTS.get("hub"):
        return _norm(_OPTS["hub"])
    if _SELECTED["url"] and not force_refresh:
        return _SELECTED["url"]
    cfg = load_config()
    res = resolve_hub(use_cache=_OPTS["use_cache"] and not force_refresh)
    if res["selected"]:
        _SELECTED["url"] = res["selected"]
        return res["selected"]
    local = find_local_hub_code(cfg)
    want_start = bool(_OPTS.get("start_local"))
    if local and not want_start and sys.stdin.isatty() and sys.stderr.isatty():
        sys.stderr.write(no_hub_message(res["tried"], local) + "\n")
        answer = input("Start a local hub on this device's Tailscale address now? [y/N] ")
        want_start = answer.strip().lower() in ("y", "yes")
        if not want_start:
            sys.exit(EXIT_NO_HUB)
    if local and want_start:
        try:
            url = start_local_hub(cfg)
        except RuntimeError as exc:
            sys.stderr.write(no_hub_message(res["tried"], local) + "\n")
            sys.exit(f"could not start a local hub: {exc}")
        print(f"[agent-memory] started local hub at {url}", file=sys.stderr)
        _SELECTED["url"] = url
        return url
    sys.stderr.write(no_hub_message(res["tried"], local) + "\n")
    sys.exit(EXIT_NO_HUB)


def _http(method: str, url: str, payload: dict | None = None, key: str = "",
          timeout: float = 30) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Shared-Key", key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def request(method: str, path: str, payload: dict | None = None) -> dict:
    key = shared_key(load_config())
    base = select_hub()
    try:
        return _http(method, base + path, payload, key)
    except urllib.error.HTTPError as exc:
        sys.exit(f"hub error HTTP {exc.code} from {base}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except (urllib.error.URLError, OSError) as exc:
        if _OPTS.get("hub"):
            sys.exit(f"cannot reach hub at {base} ({exc}). Ask the user which hub to use.")
        # The cached hub went away (e.g. the PC rebooted into the other OS):
        # forget it and re-resolve once before giving up.
        clear_cache()
        _SELECTED["url"] = None
        retry = select_hub(force_refresh=True)
        try:
            return _http(method, retry + path, payload, key)
        except urllib.error.HTTPError as exc2:
            sys.exit(f"hub error HTTP {exc2.code} from {retry}: {exc2.read().decode('utf-8', 'replace')[:200]}")
        except (urllib.error.URLError, OSError) as exc2:
            sys.exit(f"cannot reach hub at {retry} ({exc2}). Run `memory.py status`, then ask the user.")


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




def _q(pk: str) -> str:
    return urllib.parse.quote(pk, safe="")


# Directory names carry scaffolding words that bury the distinctive part of the
# name: "myapp-build" can score 0.20 against the sessions that built myapp,
# while "myapp" scores 1.00. Strip these before seeding a search.
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
    key -- myapp built on Windows can live under path:C:\\Users\\me, which
    has no substring in common with a /Volumes/.../myapp-build checkout.
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
    if d.get("related_projects") and d.get("message"):
        # A young project: its own brief is real but probably partial, and the
        # rest of the history sits under another key.
        print(f"\n## Heads-up\n{d['message']}")


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


# --------------------------------------------------------------------------
# status / doctor
# --------------------------------------------------------------------------

def spool_path() -> Path:
    return Path(os.getenv("AGENT_MEMORY_SPOOL") or (Path.home() / ".cache" / "agent-memory" / "spool.db")).expanduser()


def watcher_state_path(spool: Path | None = None) -> Path:
    return (spool or spool_path()).parent / "watcher-state.json"


def spool_backlog(path: Path | None = None) -> dict[str, Any]:
    """Read-only look at the watcher's spool. Never creates or locks it."""
    path = path or spool_path()
    info: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not info["exists"]:
        return info
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT COUNT(*), MIN(created_at), COUNT(DISTINCT session_id) FROM outbound WHERE delivered = 0"
            ).fetchone()
            info.update(pending=row[0], oldest_pending=row[1], pending_sessions=row[2])
            last = conn.execute("SELECT MAX(updated_at) FROM checkpoints").fetchone()
            info["last_scan_progress"] = last[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    return info


def _pid_alive(pid: Any) -> bool | None:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        out = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return bool(out and str(pid) in out)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def watcher_state(spool: Path | None = None) -> dict[str, Any]:
    path = watcher_state_path(spool)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"path": str(path), "found": False}
    data = data if isinstance(data, dict) else {}
    data.update(path=str(path), found=True, pid_alive=_pid_alive(data.get("pid")))
    return data


def collect_status(*, use_cache: bool = False) -> dict[str, Any]:
    cfg = load_config()
    if _OPTS.get("hub"):
        cands = [(_norm(_OPTS["hub"]), "--hub")]
    else:
        cands = candidate_hubs(cfg)
    res = resolve_hub(cands, use_cache=use_cache)
    selected = res["selected"]
    hub_counts = None
    if selected:
        try:
            st = _http("GET", selected + "/v1/status", key=shared_key(cfg), timeout=10)
            hub_counts = {k: st.get("counts", {}).get(k) for k in ("projects", "sessions", "session_events", "entries")}
        except (urllib.error.URLError, OSError, ValueError):
            hub_counts = None
    spool = spool_path()
    return {
        "config": {"path": str(config_path()), "found": config_path().is_file(),
                   "error": cfg.get("_error")},
        "shared_key": "set" if shared_key(cfg) else "not set",
        "candidates": [{"url": u, "source": s} for u, s in cands],
        "tried": res["tried"],
        "selected": selected,
        "selected_is_primary": bool(selected and cands and selected == cands[0][0]),
        "hub_counts": hub_counts,
        "local_hub_code": str(find_local_hub_code(cfg) or "") or None,
        "tailscale": tailscale_address(),
        "watcher": watcher_state(spool),
        "spool": spool_backlog(spool),
    }


def cmd_status(a):
    st = collect_status(use_cache=False)
    if not st["selected"] and _OPTS.get("start_local") and st["local_hub_code"]:
        select_hub(force_refresh=True)  # starts the local hub (or exits explaining why)
        st = collect_status(use_cache=False)
    if getattr(a, "json", False):
        print(json.dumps(st, indent=2, default=str))
        return 0 if st["selected"] else EXIT_NO_HUB
    cfg = st["config"]
    print("# agent-memory status")
    missing = "not found, using env" + (" + defaults" if DEFAULT_HUBS else "")
    print(f"config: {cfg['path']} ({'found' if cfg['found'] else missing})"
          + (f"  ERROR: {cfg['error']}" if cfg.get("error") else ""))
    print(f"shared key: {st['shared_key']}")
    print("hubs tried (in preference order):")
    print(format_tried(st["tried"]))
    if st["selected"]:
        note = "" if st["selected_is_primary"] else \
            "  (fallback: the preferred hub is down. Sessions written here must be synced to it later.)"
        print(f"selected hub: {st['selected']}{note}")
        if st["hub_counts"]:
            c = st["hub_counts"]
            print(f"hub contents: {c.get('projects')} projects, {c.get('sessions')} sessions, "
                  f"{c.get('session_events')} events, {c.get('entries')} entries")
    else:
        print("selected hub: NONE REACHABLE")
    ts = st["tailscale"]
    print(f"this device: tailscale {ts.get('ip') or 'not running'}"
          + (f" ({ts['dns']})" if ts.get("dns") else ""))
    print(f"local hub code: {st['local_hub_code'] or 'not on this machine'}")
    w = st["watcher"]
    if w.get("found"):
        alive = {True: "running", False: "not running", None: "unknown"}[w.get("pid_alive")]
        print(f"watcher: pid {w.get('pid')} {alive}; last cycle {w.get('last_cycle_at')}; "
              f"hub {w.get('hub') or 'none reachable'}; delivered {w.get('delivered')} "
              f"pending {w.get('pending')} failed {w.get('failed')}")
        if w.get("fallback_deliveries"):
            for url, n in w["fallback_deliveries"].items():
                print(f"  ! {n} events went to fallback hub {url}; when the preferred hub is back run:\n"
                      f"    memory.py sync --from {url} --to <preferred hub>")
    else:
        print(f"watcher: no state file at {w['path']} (watcher not run on this device, or an older version)")
    sp = st["spool"]
    if sp.get("exists"):
        if sp.get("error"):
            print(f"spool: {sp['path']} unreadable: {sp['error']}")
        else:
            print(f"spool backlog: {sp.get('pending', 0)} events in {sp.get('pending_sessions', 0)} sessions"
                  + (f", oldest {sp['oldest_pending']}" if sp.get("oldest_pending") else "")
                  + f"  ({sp['path']})")
    else:
        print(f"spool: none at {sp['path']}")
    if not st["selected"]:
        print("\n" + no_hub_message(st["tried"], Path(st["local_hub_code"]) if st["local_hub_code"] else None))
        return EXIT_NO_HUB
    return 0


# --------------------------------------------------------------------------
# sync: copy sessions + entries from one hub to another (idempotent)
# --------------------------------------------------------------------------

_SESSION_FIELDS = ("session_id", "project_key", "display_name", "repo_root", "git_remote", "agent_id",
                   "device_name", "tool", "cwd", "git_branch", "ai_title", "last_prompt", "status",
                   "is_sidechain", "started_at", "metadata")


def sync_hubs(src: str, dst: str, *, key: str = "", since: str | None = None,
              project_key: str | None = None, entries: bool = True, dry_run: bool = False,
              http: Callable[..., dict] = _http, log: Callable[[str], None] = print) -> dict[str, int]:
    """Push everything ``src`` has (optionally since a timestamp) into ``dst``.

    Safe to re-run: the hub dedupes events on (session_id, seq) and entries on
    (agent_id, namespace, source_id), so a second pass reports only skips.
    """
    src, dst = _norm(src), _norm(dst)
    stats = {"sessions": 0, "events_sent": 0, "inserted": 0, "skipped": 0, "entries": 0}
    offset = 0
    while True:
        q = {"limit": 200, "offset": offset}
        if since:
            q["since"] = since
        if project_key:
            q["project_key"] = project_key
        page = http("GET", f"{src}/v1/export/sessions?{urllib.parse.urlencode(q)}", key=key)
        sessions = page.get("sessions") or []
        for s in sessions:
            session = {k: s.get(k) for k in _SESSION_FIELDS if s.get(k) is not None}
            stats["sessions"] += 1
            after = -1
            while True:
                sid = urllib.parse.quote(s["session_id"], safe="")
                ev = http("GET", f"{src}/v1/export/sessions/{sid}/events?after_seq={after}&limit=1000", key=key)
                events = ev.get("events") or []
                if not events:
                    break
                stats["events_sent"] += len(events)
                if not dry_run:
                    r = http("POST", f"{dst}/v1/sessions/events", {"session": session, "events": events}, key=key)
                    stats["inserted"] += int(r.get("inserted", 0))
                    stats["skipped"] += int(r.get("skipped", 0))
                after = events[-1]["seq"]
                if len(events) < 1000:
                    break
        if page.get("next_offset") is None or not sessions:
            break
        offset = page["next_offset"]
    if entries:
        registered: set[str] = set()
        offset = 0
        while True:
            q = {"limit": 200, "offset": offset}
            if since:
                q["since"] = since
            page = http("GET", f"{src}/v1/export/entries?{urllib.parse.urlencode(q)}", key=key)
            for e in page.get("entries") or []:
                if project_key and e.get("project_key") != project_key:
                    continue
                stats["entries"] += 1
                if dry_run:
                    continue
                agent = e.get("agent") or {"agent_id": e["agent_id"], "display_name": e["agent_id"]}
                if agent["agent_id"] not in registered:
                    http("POST", f"{dst}/v1/agents/register", agent, key=key)
                    registered.add(agent["agent_id"])
                body = {k: e[k] for k in ("agent_id", "namespace", "source_id", "kind", "title", "content",
                                          "tags", "recipient_id", "visibility", "source_uri", "metadata",
                                          "project_key") if e.get(k) is not None}
                if body.get("project_key") == "__global__":
                    body.pop("project_key")
                http("POST", f"{dst}/v1/entries/upsert", body, key=key)
            if page.get("next_offset") is None or not page.get("entries"):
                break
            offset = page["next_offset"]
    log(f"sync {src} -> {dst}{' (dry run)' if dry_run else ''}: {stats['sessions']} sessions, "
        f"{stats['events_sent']} events sent ({stats['inserted']} new, {stats['skipped']} already there), "
        f"{stats['entries']} entries")
    return stats


def cmd_sync(a):
    key = shared_key(load_config())
    for url in (a.src, a.dst):
        p = probe_hub(_norm(url), key)
        if not p["ok"]:
            sys.exit(f"{url} is not reachable ({p['error']}); both hubs must be up to sync.")
    try:
        sync_hubs(a.src, a.dst, key=key, since=a.since, project_key=a.project_key,
                  entries=not a.no_entries, dry_run=a.dry_run)
    except urllib.error.HTTPError as exc:
        hint = " (the source hub is older than this client and has no /v1/export; update it first)" \
            if exc.code == 404 else ""
        sys.exit(f"sync failed: HTTP {exc.code} {exc.url}{hint}")
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"sync failed: {exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page that cannot print the bullet used in results.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(prog="memory", description="Agent Memory hub client")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--project-key")
    p.add_argument("--scope", choices=["project", "linked", "global"], default="project")
    p.add_argument("--hub", help="Use exactly this hub URL (skips resolution and fallback)")
    p.add_argument("--start-local", action="store_true",
                   help="If no hub answers and this machine has the hub code, start one on its Tailscale address")
    p.add_argument("--no-cache", action="store_true", help="Re-probe hubs instead of using the cached choice")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", aliases=["doctor"], help="Hubs tried/selected, watcher and spool state")
    st.add_argument("--json", action="store_true"); st.set_defaults(fn=cmd_status)
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
    sy = sub.add_parser("sync", help="Copy sessions/decisions from one hub to another (idempotent)")
    sy.add_argument("--from", dest="src", required=True); sy.add_argument("--to", dest="dst", required=True)
    sy.add_argument("--since", help="Only sessions/entries active since this ISO timestamp")
    sy.add_argument("--no-entries", action="store_true"); sy.add_argument("--dry-run", action="store_true")
    sy.set_defaults(fn=cmd_sync)

    a = p.parse_args(argv)
    _OPTS.update(hub=a.hub, start_local=a.start_local, use_cache=not a.no_cache)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
