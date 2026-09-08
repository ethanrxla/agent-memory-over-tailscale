"""Serve the agent-memory skill so devices can update themselves.

Every device runs the skill's ``memory.py``, but the devices sit behind
Tailscale with no inbound SSH -- the hub cannot push a new copy to them. It can
serve one, over the same tailnet connection the agent already uses, which turns
updating a device into a single command with no repo checkout and no git.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from ..context import AppContext

# An allowlist, not a directory walk: this endpoint is reachable by anything on
# the tailnet, and must never be able to read outside the skill folder.
SKILL_FILES = {
    "SKILL.md": "text/markdown; charset=utf-8",
    "memory.py": "text/x-python; charset=utf-8",
}

INSTALL_SH = """#!/usr/bin/env bash
# Install/update the agent-memory skill from the hub. Safe to re-run.
set -euo pipefail
HUB="${AGENT_MEMORY_URL:-%(hub)s}"
for dest in "$HOME/.claude/skills/agent-memory" "$HOME/.codex/skills/agent-memory"; do
  parent="$(dirname "$dest")"
  [ -d "$parent" ] || continue
  mkdir -p "$dest"
  for f in SKILL.md memory.py; do
    curl -fsSL ${AGENT_MEMORY_SHARED_KEY:+-H "X-Shared-Key: $AGENT_MEMORY_SHARED_KEY"} \\
      "$HUB/skill/agent-memory/$f" -o "$dest/$f"
  done
  chmod +x "$dest/memory.py"
  echo "  updated -> $dest"
done
echo "Set AGENT_MEMORY_URL=$HUB in your shell profile if it is not already set."
"""


def _skill_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "skill" / "agent-memory"


def register(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/skill/install.sh", response_class=PlainTextResponse)
    def install_script(_: None = Depends(ctx.require_key_dep())) -> PlainTextResponse:
        hub = ctx.config.public_url or "http://pop-os.tailf11891.ts.net:8787"
        return PlainTextResponse(
            INSTALL_SH % {"hub": hub}, media_type="text/x-shellscript; charset=utf-8"
        )

    @app.get("/skill/agent-memory/{filename:path}", response_class=PlainTextResponse)
    def skill_file(filename: str, _: None = Depends(ctx.require_key_dep())) -> PlainTextResponse:
        media_type = SKILL_FILES.get(filename)
        if media_type is None:
            raise HTTPException(status_code=404, detail="Unknown skill file")
        path = _skill_dir() / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Skill file not found")
        return PlainTextResponse(path.read_text("utf-8"), media_type=media_type)
