#!/usr/bin/env python3
"""Start a hub from this checkout, bound to a reachable (Tailscale) address.

Used by ``memory.py --start-local`` when no configured hub answers, and handy
by hand on a machine that normally is not the hub (e.g. the Windows side of a
dual-boot PC whose Pop!_OS side usually runs it):

    python scripts/start_local_hub.py                 # tailscale ip -4, port 8787
    python scripts/start_local_hub.py --host 100.x.y.z --port 8787 --db data/windows.db

Reads ``.env`` from the repo root (without overriding real environment
variables). The database is --db, else AGENT_MEMORY_DB_PATH, else
``data/agent_memory.db`` in the repo. Each hub keeps its own database; push
sessions between hubs with ``memory.py sync --from A --to B``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _tailscale_ip() -> str | None:
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.stdout.strip().splitlines()
    return lines[0].strip() if out.returncode == 0 and lines else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start a local agent-memory hub on this device")
    parser.add_argument("--host", help="Bind address (default: this device's tailscale IPv4)")
    parser.add_argument("--port", type=int, default=int(os.getenv("AGENT_MEMORY_PORT", "8787")))
    parser.add_argument("--db", help="SQLite path (default: AGENT_MEMORY_DB_PATH or data/agent_memory.db)")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    _load_dotenv(ROOT / ".env")
    host = args.host or _tailscale_ip()
    if not host:
        print("tailscale ip -4 failed; pass --host explicitly", file=sys.stderr)
        return 2
    db = args.db or os.getenv("AGENT_MEMORY_DB_PATH") or str(ROOT / "data" / "agent_memory.db")
    if db.startswith("/data/") and not Path("/data").is_dir():  # Docker path; meaningless natively
        db = str(ROOT / "data" / "agent_memory.db")
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    os.environ["AGENT_MEMORY_DB_PATH"] = str(Path(db).resolve())

    import uvicorn

    print(f"[agent-memory] hub on http://{host}:{args.port} db={os.environ['AGENT_MEMORY_DB_PATH']}", flush=True)
    uvicorn.run("main:app", host=host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
