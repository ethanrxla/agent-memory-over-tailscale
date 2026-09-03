#!/usr/bin/env bash
# Install the agent-memory skill so Claude Code / Codex agents can call it.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT_DIR/skill/agent-memory"

DEST_CLAUDE="$HOME/.claude/skills/agent-memory"
DEST_CODEX="$HOME/.codex/skills/agent-memory"

install_to() {
  local dest="$1"
  mkdir -p "$dest"
  cp "$SRC/SKILL.md" "$SRC/memory.py" "$dest/"
  chmod +x "$dest/memory.py"
  echo "  installed -> $dest"
}

echo "Installing agent-memory skill:"
install_to "$DEST_CLAUDE"
[ -d "$HOME/.codex" ] && install_to "$DEST_CODEX" || true

cat <<TXT

Done. The skill calls the hub at \$AGENT_MEMORY_URL (default http://127.0.0.1:8787).
On devices other than the hub, export it, e.g.:
  export AGENT_MEMORY_URL=http://pop-os.tailf11891.ts.net:8787

Restart your agent client (or reload skills) to pick it up, then invoke the
"agent-memory" skill or run:  python3 ~/.claude/skills/agent-memory/memory.py brief
TXT
