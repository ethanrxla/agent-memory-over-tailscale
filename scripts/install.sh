#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_URL=""
SHARED_KEY=""
OUTPUT_DIR="$ROOT_DIR/generated"
DEVICES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-url)
      BACKEND_URL="${2:-}"
      shift 2
      ;;
    --shared-key)
      SHARED_KEY="${2:-}"
      shift 2
      ;;
    --device)
      DEVICES+=("${2:-}")
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:-}"
      shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: ./scripts/install.sh --backend-url http://memory-host.tailnet.ts.net:8787 [options]

Options:
  --backend-url URL     Tailscale-reachable backend URL
  --shared-key KEY      Optional AGENT_MEMORY_SHARED_KEY value
  --device NAME         Generate configs for a device name; repeatable
  --output-dir DIR      Output directory for generated configs
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$BACKEND_URL" ]]; then
  echo "--backend-url is required" >&2
  exit 1
fi

if [[ ${#DEVICES[@]} -eq 0 ]]; then
  DEVICES+=("$(hostname)")
fi

mkdir -p "$OUTPUT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
ARGS=(
  "$ROOT_DIR/scripts/generate_client_configs.py"
  --backend-url "$BACKEND_URL"
  --shared-key "$SHARED_KEY"
  --repo-root "$ROOT_DIR"
  --output-dir "$OUTPUT_DIR"
)

for device in "${DEVICES[@]}"; do
  ARGS+=(--device "$device")
done

"$PYTHON_BIN" "${ARGS[@]}"

cat <<EOF

Generated MCP configs under:
  $OUTPUT_DIR

Next steps:
1. Open the per-device folder inside generated/.
2. Paste claude-desktop.json into Claude Desktop's MCP config.
3. Either paste codex-config.toml into ~/.codex/config.toml or run codex-mcp-add.sh.
4. Start the backend:
     cd "$ROOT_DIR"
     docker compose up -d --build

Dashboard:
  $BACKEND_URL/
EOF
