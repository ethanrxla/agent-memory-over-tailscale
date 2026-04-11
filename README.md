# Memory Agents MCP

`memory-agents-mcp` is a local-first shared memory backend plus MCP wrapper for multi-agent work.

It is meant for setups where multiple Claude or Codex instances are running independently across:

- multiple devices
- multiple terminals
- multiple repositories
- multiple concurrent tasks on the same project

The goal is simple: give those agents a shared place to publish durable facts, search prior work, and send handoff messages without relying on one agent's local context window.

## What It Provides

- A lightweight HTTP memory service backed by SQLite FTS5
- Chunked note and artifact storage for retrieval-oriented lookups
- Direct and broadcast messaging between agents
- An MCP wrapper each AI client can run locally
- A simple CLI for manual use and shell scripting
- Optional shared-key auth on top of Tailscale

## Architecture

One device runs the shared backend:

- `main.py` exposes the HTTP API
- `compose.yaml` persists the SQLite database under `./data`

Each AI client runs its own local MCP process:

- `agent_memory_mcp.py` speaks MCP locally
- the MCP wrapper forwards requests to the shared backend over Tailscale

This gives you one shared memory database across all devices and repos while keeping MCP integration local to each client.

## Main Files

- `main.py`: FastAPI memory service
- `agent_memory_mcp.py`: MCP wrapper
- `agent_memory_cli.py`: CLI helper
- `SKILLS.md`: operating rules for AI agents
- `mcp-config.example.json`: example MCP client config

## Quick Start

Run the backend on the device you want to act as the memory hub:

```bash
cd memory-agents-mcp
docker compose up -d --build
```

Point other devices at it over Tailscale:

```bash
export AGENT_MEMORY_URL="http://memory-host.tailnet.ts.net:8787"
export AGENT_MEMORY_SHARED_KEY="set-if-configured"
```

Register an agent:

```bash
python3 agent_memory_cli.py register \
  --agent-id codex-laptop \
  --display-name "Codex Laptop" \
  --device-name laptop \
  --tailscale-name laptop.tailnet.ts.net
```

Publish a durable note:

```bash
python3 agent_memory_cli.py publish \
  --agent-id codex-laptop \
  --namespace chimera \
  --source-id shannon-runtime \
  --kind artifact \
  --title "Working Shannon runtime" \
  --tag shannon \
  --content "The working container uses claude-sonnet-4-6 and is baked into the image."
```

Search prior work:

```bash
python3 agent_memory_cli.py search \
  --query "shannon runtime container model" \
  --namespace chimera \
  --requester-agent-id codex-laptop
```

## MCP Setup

Use [mcp-config.example.json](mcp-config.example.json) as the template for your AI client.

Important design point:

- the MCP wrapper runs locally on each device
- the shared memory backend can live on one Tailscale-reachable node

That means four independent agents in two different repos can all talk to the same memory store without sharing one terminal session or one git repo.

## Testing

Using an existing Python environment with `pytest` available:

```bash
PYTHONPATH=. python -m pytest -q tests/test_service.py tests/test_mcp_server.py
```

## Security Notes

- Keep the backend bound to a Tailscale-reachable host, not a public interface.
- Set `AGENT_MEMORY_SHARED_KEY` if you want an application-layer secret in addition to Tailscale access controls.
- Do not store credentials, tokens, or chain-of-thought in the shared memory database.
