#!/usr/bin/env python3
import argparse
import json
import platform
import re
from pathlib import Path


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "device"


def build_claude_config(server_name: str, repo_root: Path, backend_url: str, shared_key: str) -> str:
    payload = {
        "mcpServers": {
            server_name: {
                "command": "python3",
                "args": [str(repo_root / "agent_memory_mcp.py")],
                "env": {
                    "AGENT_MEMORY_URL": backend_url,
                    "AGENT_MEMORY_SHARED_KEY": shared_key,
                },
            }
        }
    }
    return json.dumps(payload, indent=2)


def build_codex_toml(server_name: str, repo_root: Path, backend_url: str, shared_key: str) -> str:
    return "\n".join(
        [
            f"[mcp_servers.{server_name}]",
            'command = "python3"',
            f'args = ["{repo_root / "agent_memory_mcp.py"}"]',
            "",
            f"[mcp_servers.{server_name}.env]",
            f'AGENT_MEMORY_URL = "{backend_url}"',
            f'AGENT_MEMORY_SHARED_KEY = "{shared_key}"',
            "",
        ]
    )


def build_codex_add_command(server_name: str, repo_root: Path, backend_url: str, shared_key: str) -> str:
    return "\n".join(
        [
            "codex mcp add "
            f"{server_name} "
            f"--env AGENT_MEMORY_URL={backend_url} "
            f"--env AGENT_MEMORY_SHARED_KEY={shared_key} "
            f"-- python3 {repo_root / 'agent_memory_mcp.py'}",
            "",
        ]
    )


def build_claude_instructions(server_name: str) -> str:
    return "\n".join(
        [
            "Claude Desktop:",
            "1. Open Settings.",
            "2. Open Developer > Edit Config.",
            "3. Merge the JSON block below into the top-level mcpServers object.",
            f"4. Restart Claude Desktop to load `{server_name}`.",
            "",
        ]
    )


def build_codex_instructions(server_name: str) -> str:
    return "\n".join(
        [
            "Codex CLI:",
            "1. Either paste the TOML block into ~/.codex/config.toml or run the add command.",
            f"2. Verify with `codex mcp get {server_name}`.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate device-specific Claude Desktop and Codex MCP configs")
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--shared-key", default="")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--device", action="append", dest="devices")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir or repo_root / "generated").resolve()
    devices = args.devices or [platform.node() or "device"]
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict[str, str]] = {}
    for device in devices:
        device_slug = slugify(device)
        server_name = f"agent-memory-{device_slug}"
        device_dir = output_dir / device_slug
        device_dir.mkdir(parents=True, exist_ok=True)

        claude_json = build_claude_config(server_name, repo_root, args.backend_url, args.shared_key)
        codex_toml = build_codex_toml(server_name, repo_root, args.backend_url, args.shared_key)
        codex_add = build_codex_add_command(server_name, repo_root, args.backend_url, args.shared_key)

        (device_dir / "claude-desktop.json").write_text(claude_json + "\n", encoding="utf-8")
        (device_dir / "codex-config.toml").write_text(codex_toml, encoding="utf-8")
        (device_dir / "codex-mcp-add.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + codex_add, encoding="utf-8")
        (device_dir / "instructions.txt").write_text(
            build_claude_instructions(server_name) + build_codex_instructions(server_name),
            encoding="utf-8",
        )

        manifest[device] = {
            "server_name": server_name,
            "claude_desktop": str(device_dir / "claude-desktop.json"),
            "codex_toml": str(device_dir / "codex-config.toml"),
            "codex_add": str(device_dir / "codex-mcp-add.sh"),
        }

    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
