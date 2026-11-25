"""Hub resolution shared by memory.py, the watcher and the MCP server.

The owner's always-on hub lives on one OS of a dual-boot PC. When the other OS
is booted that hub is simply gone, and clients that hard-coded it failed (or
the agent guessed). These tests pin the contract: probe candidates in order,
use the first healthy one, fall back, and when nothing answers fail loudly
with the list tried -- starting a local hub only behind an explicit flag.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_memory.hubs import load_resolver
from watcher.daemon import build_client, deliver, write_state
from watcher.hub_client import ResolvingHubClient
from watcher.spool import Spool

memory: Any = load_resolver()
A, B, C = "http://hub-a:8787", "http://hub-b:8787", "http://hub-c:8787"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own state dir, no env hub, no cache, fresh opts."""
    monkeypatch.setenv("AGENT_MEMORY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGENT_MEMORY_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_URL", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_SHARED_KEY", raising=False)
    monkeypatch.setenv("AGENT_MEMORY_SPOOL", str(tmp_path / "spool" / "spool.db"))
    memory._OPTS.update(hub=None, start_local=False, use_cache=True)
    memory._SELECTED["url"] = None
    yield
    memory._OPTS.update(hub=None, start_local=False, use_cache=True)
    memory._SELECTED["url"] = None


def _write_config(tmp_path: Path, data: dict) -> None:
    path = tmp_path / "state" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def fake_probe(up: set[str], calls: list[str] | None = None):
    def probe(url, key="", timeout=None):
        if calls is not None:
            calls.append(url)
        ok = url in up
        return {"url": url, "ok": ok, "error": None if ok else "timed out after 2.5s", "ms": 1}
    return probe


# -- candidates -----------------------------------------------------------

def test_candidates_order_env_then_config_then_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, {"hubs": [B, A + "/"]})
    env = {"AGENT_MEMORY_URL": f"{A}, {C}"}
    cands = memory.candidate_hubs(memory.load_config(), env)
    urls = [u for u, _ in cands]
    assert urls[:3] == [A, C, B]  # env (comma list), then config; A de-duplicated
    assert urls[3:] == list(memory.DEFAULT_HUBS)
    assert dict(cands)[A] == "env AGENT_MEMORY_URL" and dict(cands)[B] == "config"


def test_config_can_drop_defaults(tmp_path: Path) -> None:
    _write_config(tmp_path, {"hubs": [B], "include_defaults": False})
    assert memory.candidate_hubs(memory.load_config(), {}) == [(B, "config")]


def test_no_hubs_are_built_in() -> None:
    # The skill ships to other people's machines: it must never probe the
    # author's own hubs. With nothing configured there is nothing to try.
    assert memory.DEFAULT_HUBS == ()
    assert memory.candidate_hubs({}, {}) == []
    assert "none configured" in memory.format_tried([])


def test_shared_key_is_never_read_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, {"shared_key": "from-file"})
    assert memory.shared_key(memory.load_config()) == ""
    monkeypatch.setenv("AGENT_MEMORY_SHARED_KEY", "from-env")
    assert memory.shared_key(memory.load_config()) == "from-env"


# -- resolution -----------------------------------------------------------

def test_first_healthy_candidate_wins() -> None:
    res = memory.resolve_hub([(A, "x"), (B, "x")], probe=fake_probe({A, B}))
    assert res["selected"] == A


def test_falls_back_when_preferred_is_down() -> None:
    res = memory.resolve_hub([(A, "x"), (B, "x"), (C, "x")], probe=fake_probe({B, C}))
    assert res["selected"] == B
    assert [t["ok"] for t in res["tried"]] == [False, True, True]


def test_choice_is_cached_briefly() -> None:
    calls: list[str] = []
    cands = [(A, "x"), (B, "x")]
    assert memory.resolve_hub(cands, probe=fake_probe({B}, calls))["selected"] == B
    n = len(calls)
    res = memory.resolve_hub(cands, probe=fake_probe(set(), calls))
    assert res["selected"] == B and res["cached"] and len(calls) == n
    # Expired cache re-probes.
    later = memory.resolve_hub(cands, probe=fake_probe({A}, calls), now=lambda: 10**12)
    assert later["selected"] == A and not later["cached"]


def test_none_reachable_gives_clear_error() -> None:
    res = memory.resolve_hub([(A, "env"), (B, "default")], probe=fake_probe(set()))
    assert res["selected"] is None
    msg = memory.no_hub_message(res["tried"], None)
    assert A in msg and B in msg and "timed out" in msg
    assert "ASK THE USER" in msg
    msg_local = memory.no_hub_message(res["tried"], Path("/repo"))
    assert "--start-local" in msg_local and "ASK THE USER" in msg_local


def test_cli_exits_nonzero_and_lists_hubs_when_none_reachable(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_MEMORY_URL", A)
    monkeypatch.setattr(memory, "probe_hub", fake_probe(set()))
    monkeypatch.setattr(memory, "find_local_hub_code", lambda cfg=None: None)
    with pytest.raises(SystemExit) as exc:
        memory.main(["--cwd", ".", "sessions"])
    assert exc.value.code == memory.EXIT_NO_HUB
    err = capsys.readouterr().err
    assert A in err and "ASK THE USER" in err


def test_request_reresolves_when_cached_hub_disappears(monkeypatch) -> None:
    up = {A}
    monkeypatch.setattr(memory, "probe_hub", lambda u, k="", t=None: {"url": u, "ok": u in up, "error": "down", "ms": 1})
    monkeypatch.setenv("AGENT_MEMORY_URL", f"{A},{B}")
    assert memory.select_hub() == A
    seen: list[str] = []

    def http(method, url, payload=None, key="", timeout=30):
        seen.append(url)
        if url.startswith(A):
            raise memory.urllib.error.URLError("connection refused")
        return {"ok": True}

    monkeypatch.setattr(memory, "_http", http)
    up.clear(); up.add(B)  # the PC rebooted into the other OS
    assert memory.request("GET", "/v1/projects") == {"ok": True}
    assert seen == [A + "/v1/projects", B + "/v1/projects"]


# -- starting a local hub (behind a flag) ----------------------------------

def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "agent_memory").mkdir(parents=True)
    (repo / "agent_memory" / "app.py").write_text("")
    (repo / "main.py").write_text("")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "start_local_hub.py").write_text("")
    return repo


def test_start_local_hub_binds_tailscale_address(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    launched: list[list[str]] = []
    url = memory.start_local_hub(
        {"local_hub": {"repo": str(repo), "python": "py"}},
        launcher=lambda cmd, **kw: launched.append(cmd),
        probe=lambda u, k="", t=None: {"url": u, "ok": True},
        address=lambda: {"ip": "100.1.2.3", "dns": "box.tail.ts.net"},
        sleep=lambda s: None,
    )
    assert url == "http://box.tail.ts.net:8787"
    assert launched[0][:2] == ["py", str(repo / "scripts" / "start_local_hub.py")]
    assert launched[0][launched[0].index("--host") + 1] == "100.1.2.3"
    assert memory.read_cache()["url"] == url


def test_start_local_hub_refuses_without_tailscale(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    with pytest.raises(RuntimeError, match="Tailscale"):
        memory.start_local_hub({"local_hub": {"repo": str(repo)}}, launcher=lambda *a, **k: None,
                               address=lambda: {"ip": None, "dns": None})


def test_local_hub_only_started_with_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AGENT_MEMORY_URL", A)
    _write_config(tmp_path, {"include_defaults": False})
    monkeypatch.setattr(memory, "probe_hub", fake_probe(set()))
    monkeypatch.setattr(memory, "find_local_hub_code", lambda cfg=None: tmp_path)
    started: list[bool] = []
    monkeypatch.setattr(memory, "start_local_hub", lambda cfg=None, **k: started.append(True) or "http://me:8787")

    with pytest.raises(SystemExit):  # no flag (and no TTY): never auto-start
        memory.select_hub()
    assert not started and "--start-local" in capsys.readouterr().err

    memory._OPTS["start_local"] = True
    assert memory.select_hub() == "http://me:8787" and started == [True]


# -- real HTTP probing ------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.server.require_key and self.headers.get("X-Shared-Key") != "k":  # type: ignore[attr-defined]
            self.send_response(401); self.end_headers(); return
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def http_hub():
    servers = []

    def make(require_key=False):
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        srv.require_key = require_key  # type: ignore[attr-defined]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield make
    for s in servers:
        s.shutdown()


def test_probe_hub_real_http(http_hub) -> None:
    open_url, keyed = http_hub(), http_hub(require_key=True)
    assert memory.probe_hub(open_url)["ok"]
    bad = memory.probe_hub(keyed, key="wrong")
    assert not bad["ok"] and "401" in bad["error"] and "shared key" in bad["error"]
    assert memory.probe_hub(keyed, key="k")["ok"]
    dead = memory.probe_hub("http://127.0.0.1:9", timeout=1)
    assert not dead["ok"] and dead["error"]


# -- status -------------------------------------------------------------------

def test_status_reports_hubs_watcher_and_spool_without_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MEMORY_SHARED_KEY", "super-secret-value")
    monkeypatch.setenv("AGENT_MEMORY_URL", f"{A},{B}")
    monkeypatch.setattr(memory, "probe_hub", fake_probe({B}))
    monkeypatch.setattr(memory, "_http", lambda *a, **k: {"counts": {"sessions": 7}})
    monkeypatch.setattr(memory, "tailscale_address", lambda: {"ip": "100.1.1.1", "dns": None})
    spool = Spool(memory.spool_path())
    spool.enqueue("s1", 0, {"x": 1}); spool.enqueue("s1", 1, {"x": 2}); spool.commit()
    (memory.spool_path().parent / "watcher-state.json").write_text(json.dumps(
        {"pid": 0, "hub": B, "fallback_deliveries": {B: 5}}))
    spool.close()

    out = io.StringIO()
    with redirect_stdout(out):
        code = memory.main(["status", "--json"])
    st = json.loads(out.getvalue())
    assert code == 0 and st["selected"] == B and st["selected_is_primary"] is False
    assert [t["url"] for t in st["tried"]][:2] == [A, B]
    assert st["spool"]["pending"] == 2 and st["watcher"]["found"]
    assert st["shared_key"] == "set"

    text = io.StringIO()
    with redirect_stdout(text):
        memory.main(["status"])
    printed = text.getvalue()
    assert "super-secret-value" not in out.getvalue() + printed
    assert "selected hub: " + B in printed and "spool backlog: 2 events" in printed
    assert f"memory.py sync --from {B}" in printed


def test_status_exit_code_when_nothing_reachable(monkeypatch) -> None:
    monkeypatch.setattr(memory, "probe_hub", fake_probe(set()))
    monkeypatch.setattr(memory, "tailscale_address", lambda: {"ip": None, "dns": None})
    with redirect_stdout(io.StringIO()) as out:
        assert memory.main(["status"]) == memory.EXIT_NO_HUB
    assert "NONE REACHABLE" in out.getvalue()


# -- watcher ------------------------------------------------------------------

class _Recording(ResolvingHubClient):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.sent: list[tuple[str, int]] = []

    def send_events(self, session, events):
        self.sent.append((self.base_url, len(events)))
        return {"inserted": len(events)}


def test_watcher_spools_until_a_hub_comes_up_then_delivers_there(tmp_path: Path) -> None:
    up: set[str] = set()
    client = _Recording([(A, "env"), (B, "default")],
                        resolve=lambda c, **k: memory.resolve_hub(c, use_cache=False, probe=fake_probe(up)))
    spool = Spool(tmp_path / "s.db")
    for seq in range(3):
        spool.enqueue("sess", seq, {"session": {"session_id": "sess"}, "event": {"seq": seq}})
    spool.commit()

    stats = deliver(spool, client)
    assert stats["delivered"] == 0 and spool.pending_count() == 3  # nothing dropped

    up.add(B)  # only the fallback hub is up
    stats = deliver(spool, client)
    assert stats["delivered"] == 3 and client.sent == [(B, 3)] and spool.pending_count() == 0
    state: dict = {}
    write_state(spool, client, {**stats, "pending": 0}, state)
    saved = json.loads((tmp_path / "watcher-state.json").read_text())
    assert saved["hub"] == B and saved["preferred_hub"] == A and saved["fallback_deliveries"] == {B: 3}

    up.add(A)  # preferred hub back: next cycle goes there
    spool.enqueue("sess", 9, {"session": {"session_id": "sess"}, "event": {"seq": 9}}); spool.commit()
    deliver(spool, client)
    assert client.sent[-1] == (A, 1)
    spool.close()


def test_watcher_runs_offline_when_no_hub_is_configured(tmp_path: Path) -> None:
    # A fresh install has no built-in hubs: the watcher must still start and
    # spool transcripts until a hub is configured, not crash on launch.
    client = build_client(None, "")
    assert client.candidates == [] and client.health() is False
    spool = Spool(tmp_path / "s.db")
    spool.enqueue("sess", 1, {"session": {"session_id": "sess"}, "event": {"seq": 1}}); spool.commit()
    stats = deliver(spool, client)
    assert stats["delivered"] == 0 and spool.pending_count() == 1
    state: dict = {}
    write_state(spool, client, {**stats, "pending": 1}, state)
    saved = json.loads((tmp_path / "watcher-state.json").read_text())
    assert saved["hub"] is None and saved["candidates"] == []
    spool.close()


def test_offline_watcher_picks_up_a_hub_added_to_config(tmp_path: Path, monkeypatch) -> None:
    # Installed before any hub was configured: the same running client must
    # start delivering once a hub is added to config.json, without a restart.
    monkeypatch.setattr(memory, "probe_hub", fake_probe({A, B}))
    client = build_client(None, "")
    pinned = build_client(None, "", no_fallback=True)
    sent: list[tuple[str, int]] = []
    client.send_events = lambda session, events: sent.append((client.base_url, len(events))) or {}
    spool = Spool(tmp_path / "s.db")
    spool.enqueue("sess", 1, {"session": {"session_id": "sess"}, "event": {"seq": 1}}); spool.commit()
    assert deliver(spool, client)["delivered"] == 0 and spool.pending_count() == 1

    _write_config(tmp_path, {"hubs": [A, B]})
    stats = deliver(spool, client)
    assert stats["delivered"] == 1 and sent == [(A, 1)] and spool.pending_count() == 0
    assert client.primary == A
    assert pinned.health() is True and pinned.candidates == [(A, "config")]  # --no-fallback still pins one
    spool.close()


def test_status_start_local_reports_the_started_hub(tmp_path: Path, monkeypatch) -> None:
    started = "http://me:8787"
    monkeypatch.setattr(memory, "probe_hub", fake_probe({started}))
    monkeypatch.setattr(memory, "find_local_hub_code", lambda cfg=None: tmp_path)
    monkeypatch.setattr(memory, "start_local_hub", lambda cfg=None, **k: started)
    monkeypatch.setattr(memory, "tailscale_address", lambda: {"ip": None, "dns": None})
    monkeypatch.setattr(memory, "_http", lambda *a, **k: {"counts": {}})
    with redirect_stdout(io.StringIO()) as out:
        assert memory.main(["--start-local", "status"]) == 0
    assert "selected hub: " + started in out.getvalue()
    assert memory.candidate_hubs(memory.load_config(), {}) == []  # nothing persisted


def test_watcher_build_client_uses_shared_candidates(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MEMORY_URL", raising=False)
    client = build_client(C, "")
    urls = [u for u, _ in client.candidates]
    assert urls[0] == C and urls[1:] == list(memory.DEFAULT_HUBS)
    assert [u for u, _ in build_client(C, "", no_fallback=True).candidates] == [C]


def test_mcp_client_resolves_and_raises_ask_user(monkeypatch) -> None:
    from agent_memory_mcp import AgentMemoryHttpClient

    monkeypatch.setenv("AGENT_MEMORY_URL", A)
    monkeypatch.setattr(memory, "probe_hub", fake_probe(set()))
    monkeypatch.setattr(memory, "find_local_hub_code", lambda cfg=None: None)
    client = AgentMemoryHttpClient()
    with pytest.raises(RuntimeError, match="ASK THE USER"):
        client.request_json("GET", "/health")
    monkeypatch.setattr(memory, "probe_hub", fake_probe({B}))
    monkeypatch.setenv("AGENT_MEMORY_URL", f"{A},{B}")
    assert AgentMemoryHttpClient().base_url == B
    assert AgentMemoryHttpClient(base_url=C + "/").base_url == C  # explicit pins


def test_spool_backlog_is_read_only(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "spool.db"
    assert memory.spool_backlog(missing) == {"path": str(missing), "exists": False}
    assert not missing.exists()
    db = tmp_path / "s.db"
    sqlite3.connect(db).close()  # exists but has no tables
    assert "error" in memory.spool_backlog(db)
