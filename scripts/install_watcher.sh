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
      echo "Usage: $0 --backend-url http://hub-a.tailnet.ts.net:8787[,http://hub-b...] [--shared-key KEY] [--agent-id ID] [--deny PATH] [--backfill]"
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

# The watcher imports the hub package (for project keys), so it needs the hub's
# Python deps. Fail here rather than in a service that restarts forever.
if ! ( cd "$ROOT_DIR" && python3 -c "import watcher.daemon" ) 2>/dev/null; then
  echo "python3 cannot import the watcher. Install deps first:" >&2
  echo "  python3 -m pip install --user -r $ROOT_DIR/requirements.txt" >&2
  exit 1
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  # macOS has no systemd: run as a launchd agent that starts at login and is
  # restarted if it dies.
  LABEL="com.agent-memory.watcher"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  LOG="$HOME/Library/Logs/agent-memory-watcher.log"
  PYTHON="$(command -v python3)"
  xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }
  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$PYTHON")</string>
    <string>-m</string><string>watcher.daemon</string>
    <string>--device</string><string>$(xml_escape "$(hostname -s)")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$ROOT_DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AGENT_MEMORY_URL</key><string>$(xml_escape "$BACKEND_URL")</string>
    <key>AGENT_MEMORY_SHARED_KEY</key><string>$(xml_escape "$SHARED_KEY")</string>
    <key>AGENT_MEMORY_AGENT_ID</key><string>$(xml_escape "$AGENT_ID")</string>
    <key>AGENT_MEMORY_DENY</key><string>$(xml_escape "$DENY")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG")</string>
</dict>
</plist>
PLIST
  chmod 600 "$PLIST"   # holds the shared key
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Watcher installed as a launchd agent (starts at login). Logs: tail -f $LOG"
  exit 0
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
