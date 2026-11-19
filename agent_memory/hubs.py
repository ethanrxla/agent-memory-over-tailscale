"""Shared hub resolution for the watcher, the MCP server and the repo CLI.

The single implementation lives in ``skill/agent-memory/memory.py`` because
that file is distributed on its own (copied into skill folders, served by the
hub) and must stay stdlib-only and self-contained. Everything else in the repo
loads it from there by path, so the three clients can never disagree about
which hub to use.

This module deliberately imports nothing from the ``agent_memory`` package, so
``load_resolver`` can also be used via ``importlib`` by path without FastAPI.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

RESOLVER_PATH = Path(__file__).resolve().parents[1] / "skill" / "agent-memory" / "memory.py"
_MODULE_NAME = "agent_memory_skill_client"


def load_resolver(path: Path | None = None) -> ModuleType | None:
    """Import the skill client as a module (cached). None if it is missing."""
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    path = path or RESOLVER_PATH
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - a broken skill file must not take the watcher down
        sys.modules.pop(_MODULE_NAME, None)
        return None
    return module
