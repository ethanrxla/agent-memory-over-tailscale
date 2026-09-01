# Shared Agent Memory — Operating Guide

This is the operator/reference guide. The **installable agent skill** lives at
[`skill/agent-memory/SKILL.md`](skill/agent-memory/SKILL.md); install it to
`~/.claude/skills/agent-memory/` (or point your client's skills dir at it) so
agents load it automatically.

## Model of operation

- One **hub** runs on an always-on Tailscale node and owns the SQLite database.
- A **watcher** runs on each device, tailing Claude Code and Codex transcripts,
  redacting secrets locally, and shipping compact session events to the hub.
- A **background worker** on the hub embeds new content and summarises sessions
  (via free NVIDIA NIM, with extractive fallback).
- Agents talk to the hub through the **MCP wrapper** (local per device) or the
  **CLI**.

## The workflow an agent should follow

1. **Brief once at session start**, scoped to the current project:
   ```bash
   python3 agent_memory_cli.py brief --cwd "$(pwd)"
   ```
2. **Search on demand**, scoped to the current project:
   ```bash
   python3 agent_memory_cli.py search --query "shannon runtime model" --cwd "$(pwd)"
   ```
3. **Record decisions** that future work must respect:
   ```bash
   python3 agent_memory_cli.py decision --agent-id codex-laptop --cwd "$(pwd)" \
     --title "..." --content "..." --rationale "..."
   ```
4. **Publish durable facts** (paths, hashes, ports, repro steps) with
   `publish_memory` / `agent_memory_cli.py publish`.
5. **Hand off** with `send_message` and read `read_inbox`.

Routine activity is captured automatically by the watcher, so reserve explicit
writes for conclusions and decisions.

## Scoping

Every memory has a `project_key` derived from the git remote (or path) of its
`cwd`. Search/brief are project-scoped by default. Use `--scope linked` (after
linking projects via `POST /v1/projects/link`) or `--scope global` to widen.
A tunable relevance floor returns "no relevant context" instead of weak matches.

## What not to store

- Secrets, API keys, tokens, credentials (redacted on-device, but don't rely on
  it).
- Hidden chain-of-thought (thinking blocks are dropped at ingest).
- Large source files when a path plus a summary will do.

## MCP tools

Session/RAG: `get_project_brief`, `search_memory`, `list_sessions`,
`get_session`, `record_decision`, `open_threads`.
Original: `register_agent`, `list_agents`, `publish_memory`, `get_entry`,
`send_message`, `read_inbox`.
Resources: `memory://skills`, `memory://service-info`.

## Tailscale

Run the hub on one node reachable over Tailscale; point `AGENT_MEMORY_URL` at
its Tailscale DNS name. Keep it off public interfaces and set
`AGENT_MEMORY_SHARED_KEY` for an extra gate.
