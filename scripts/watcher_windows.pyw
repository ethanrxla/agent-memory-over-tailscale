"""Windows launcher for the transcript watcher (run with pythonw.exe).

Used by the "AgentMemoryWatcher" Scheduled Task (see
scripts/install_watcher_windows.ps1). pythonw has no console, so output goes
to %USERPROFILE%\\.cache\\agent-memory\\watcher.log (rotated at ~5 MB), and a
lock file keeps a second logon/trigger from starting a duplicate watcher.

The hub is resolved like every other client (watcher.daemon.build_client):
AGENT_MEMORY_URL (user-level env var; comma-separated allowed) is the preferred
hub, then ``hubs`` from ~/.agent-memory/config.json (no hubs are built in).
Each cycle delivers to the first one answering /health and keeps spooling
while none does, or while none is configured yet. The shared key, if used, comes only from the
AGENT_MEMORY_SHARED_KEY user env var. Extra CLI args are passed through
(e.g. --no-fallback to pin AGENT_MEMORY_URL).
"""

from __future__ import annotations

import msvcrt
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

CACHE = Path.home() / ".cache" / "agent-memory"
CACHE.mkdir(parents=True, exist_ok=True)
LOG = CACHE / "watcher.log"
if LOG.exists() and LOG.stat().st_size > 5_000_000:
    LOG.replace(LOG.with_suffix(".log.1"))

lock = open(CACHE / "watcher.lock", "w")
try:
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    sys.exit(0)  # another watcher already running

log = open(LOG, "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log

from watcher.daemon import main  # noqa: E402

args = sys.argv[1:]
if "--device" not in args:
    args = ["--device", os.getenv("AGENT_MEMORY_DEVICE", platform.node())] + args
raise SystemExit(main(args))
