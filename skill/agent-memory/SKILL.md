---
name: agent-memory
description: >-
  Shared, project-scoped memory across all your Claude and Codex sessions and
  devices. Use at the START of a coding session to load a compact brief of what
  prior sessions did in THIS project, and mid-task to search past work by
  meaning instead of re-reading transcripts. Triggers when picking up existing
  work, wondering "have we done this before / why did we choose X", handing off
  between agents, or to avoid re-deriving context established elsewhere.
---

# Shared Agent Memory

One memory store shared by every Claude/Codex session on your Tailscale
network. Instead of re-reading a whole transcript to recover context, you pull a
short, project-scoped brief and search prior work semantically. It also lets
independent agents on different devices see each other's progress.

All commands go through the bundled `memory.py` (stdlib only — no dependencies).
Run it from this skill's directory. It reads the hub URL from `AGENT_MEMORY_URL`
(default `http://127.0.0.1:8787`; on other devices set it to the hub, e.g.
`http://pop-os.tailf11891.ts.net:8787`).

## At the start of a coding session — do this first

```bash
python3 memory.py brief
```

Scope is derived automatically from the current directory (git remote, else
path), so the brief covers only THIS project: recent session summaries,
decisions in effect, and open threads — usually a few hundred tokens, far
cheaper than reading files or scrollback. If it reports no sessions, it lists
related projects; otherwise start fresh — don't invent backstory.

## During the task

```bash
python3 memory.py search "why did we drop the shannon runtime"   # this project
python3 memory.py threads                                        # unfinished work
python3 memory.py sessions                                       # recent sessions here
python3 memory.py global-search "nvidia nim rate limit"          # ALL projects
```

Search is semantic + keyword, scoped to the current project by default. If it
returns "No relevant prior context", trust that and proceed — a forced weak
match is what sends you off track. Widen only deliberately with `global-search`
or `--scope linked`.

## When you make a decision worth keeping

```bash
python3 memory.py decide "Use sqlite-vec, not Mongo" \
  "Self-hosted Mongo has no vector search." \
  --rationale "Keeps everything in one file on the tailnet."
```

Pin choices future sessions must respect (a library, an architecture, a
"we tried X and it failed"). This is what stops the next agent re-litigating
settled questions. Routine activity is captured automatically by the watcher —
reserve explicit writes for conclusions and decisions.

## Never store

Secrets, API keys, tokens, credentials — and never your hidden chain-of-thought.
The watcher redacts known secret shapes on the source device, but don't rely on
it: keep secrets out of what you write.

## MCP alternative

If your client has the `agent-memory` MCP server configured, the same
operations are available as tools (`get_project_brief`, `search_memory`,
`record_decision`, `list_sessions`, `open_threads`) — use those instead of the
CLI when present. The CLI always works and needs no MCP setup.
