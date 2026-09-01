#!/usr/bin/env bash
# Install and start the per-device transcript watcher as a systemd --user service.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_URL="${AGENT_MEMORY_URL:-}"
SHARED_KEY="${AGENT_MEMORY_SHARED_KEY:-}"
AGENT_ID="${AGENT_MEMORY_AGENT_ID:-$(hostname)-watcher}"
DENY="${AGENT_MEMORY_DENY:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-url) BACKEND_URL="$2"; shift 2 ;;
    --shared-key)  SHARED_KEY="$2";  shift 2 ;;
    --agent-id)    AGENT_ID="$2";    shift 2 ;;
    --deny)        DENY="${DENY}:${2}"; shift 2 ;;
    --backfill)    BACKFILL=1;       shift ;;
    -h|--help)
      echo "Usage: $0 --backend-url http://host.tailnet.ts.net:8787 [--shared-key KEY] [--agent-id ID] [--deny PATH] [--backfill]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$BACKEND_URL" ]]; then
  echo "--backend-url (or AGENT_MEMORY_URL) is required" >&2
  exit 1
fi

if [[ "${BACKFILL:-0}" == "1" ]]; then
  echo "Backfilling existing transcripts into $BACKEND_URL ..."
  # `python3 -m watcher.daemon` needs the repo root on the module path.
  ( cd "$ROOT_DIR" && AGENT_MEMORY_URL="$BACKEND_URL" AGENT_MEMORY_SHARED_KEY="$SHARED_KEY" \
      python3 -m watcher.daemon --backend-url "$BACKEND_URL" --shared-key "$SHARED_KEY" \
        --agent-id "$AGENT_ID" --deny "$DENY" --backfill )
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed \
  -e "s#%h/agent-memory-over-tailscale#$ROOT_DIR#g" \
  -e "s#Environment=AGENT_MEMORY_URL=.*#Environment=AGENT_MEMORY_URL=$BACKEND_URL#" \
  -e "s#Environment=AGENT_MEMORY_SHARED_KEY=.*#Environment=AGENT_MEMORY_SHARED_KEY=$SHARED_KEY#" \
  -e "s#Environment=AGENT_MEMORY_AGENT_ID=.*#Environment=AGENT_MEMORY_AGENT_ID=$AGENT_ID#" \
  -e "s#Environment=AGENT_MEMORY_DENY=.*#Environment=AGENT_MEMORY_DENY=$DENY#" \
  "$ROOT_DIR/deploy/agent-memory-watcher.service" > "$UNIT_DIR/agent-memory-watcher.service"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now agent-memory-watcher.service
  echo "Watcher installed and started. Logs: journalctl --user -u agent-memory-watcher -f"
else
  echo "systemd not available; run manually:"
  echo "  python3 -m watcher.daemon --backend-url $BACKEND_URL"
fi
