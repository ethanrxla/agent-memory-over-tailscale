# Shared Agent Memory Skill

Use this service as a shared memory layer between Claude/Codex instances on your Tailscale network.

## Rules

1. Register yourself once per device/session before publishing data.
2. Search before asking another agent to redo work.
3. Publish durable facts, not chatty thought streams.
4. Send direct messages only for handoffs or questions that need a specific recipient.
5. Do not store API keys, passwords, bearer tokens, or raw secrets.

## What To Store

- File paths, commands, diffs, artifact locations, hashes, URLs, ports, and precise observations.
- Short summaries of failures, fixes, environment mismatches, and repro steps.
- Structured handoff notes between agents working on the same namespace.

## What Not To Store

- Hidden chain-of-thought.
- Large copied source files when a path and summary is enough.
- Secrets or private credentials.

## Namespaces

Use one namespace per project or engagement, for example:

- `chimera`
- `juice-shop`
- `scholarcal`

## Standard Workflow

### 1. Register

```bash
export AGENT_MEMORY_URL="http://<tailscale-hostname-or-ip>:8787"
export AGENT_MEMORY_SHARED_KEY="<shared-key-if-configured>"

python3 agent_memory_cli.py register \
  --agent-id codex-laptop \
  --display-name "Codex on laptop" \
  --device-name laptop \
  --tailscale-name laptop.tailnet.ts.net \
  --capability python \
  --capability review
```

### 2. Search First

```bash
python3 agent_memory_cli.py search \
  --query "shannon container model runtime" \
  --namespace chimera \
  --requester-agent-id codex-laptop
```

### 3. Publish Durable Context

```bash
python3 agent_memory_cli.py publish \
  --agent-id codex-laptop \
  --namespace chimera \
  --source-id shannon-runtime-2026-04-11 \
  --kind artifact \
  --title "Working Shannon runtime is baked Anthropic-only container" \
  --tag shannon \
  --tag runtime \
  --content "Image sha256:... uses SHANNON_MODEL=claude-sonnet-4-6 and does not bind-mount current source."
```

### 4. Send A Handoff

```bash
python3 agent_memory_cli.py send \
  --sender-agent-id codex-laptop \
  --recipient-id claude-desktop \
  --namespace chimera \
  --title "Need parity check on second machine" \
  --content "Compare the running Shannon image and env against source machine snapshot in shannon-runtime-snapshot/."
```

### 5. Read Inbox

```bash
python3 agent_memory_cli.py inbox \
  --agent-id claude-desktop \
  --namespace chimera
```

## Entry Style

- Title: one sentence, specific.
- Content: 3-10 lines, dense with facts.
- Tags: 1-5 lowercase tags.
- Source ID: stable identifier if the note may be updated later.

## Retrieval Guidance

- Search with concrete nouns first: tool names, run IDs, ports, filenames, domains.
- Narrow by namespace whenever possible.
- Use `artifact` for environment snapshots, logs, and paths.
- Use `note` for conclusions.
- Use `message` for direct handoffs.

## Tailscale Guidance

- Run the service on one node that other devices can reach over Tailscale.
- Point `AGENT_MEMORY_URL` at the node's Tailscale DNS name or IP.
- Keep the service behind Tailscale and set `AGENT_MEMORY_SHARED_KEY` if you want an extra application-layer gate.

## MCP Usage

If your AI client supports MCP, run the wrapper locally on each device and point it at the same Tailscale-hosted backend.

Example config shape is in `mcp-config.example.json`.

The wrapper exposes these tools:

- `register_agent`
- `list_agents`
- `publish_memory`
- `search_memory`
- `get_entry`
- `send_message`
- `read_inbox`

It also exposes these resources:

- `memory://skills`
- `memory://service-info`

Important: the MCP server is local to each AI client, but the memory backend can be remote over Tailscale. That means every device can share one database without needing a local model or a separate local database on each machine.
