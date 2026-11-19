---
name: agent-memory
description: >-
  Shared, project-scoped memory across all your Claude, Codex and Hermes
  sessions and devices. Use at the START of a coding session to load a compact
  brief of what prior sessions did in THIS project, and mid-task to search past
  work by meaning instead of re-reading transcripts. Triggers when picking up
  existing work, wondering "have we done this before / why did we choose X",
  handing off between agents, or to avoid re-deriving context established
  elsewhere.
---

# Shared Agent Memory

One memory store shared by every Claude/Codex/Hermes session on the Tailscale
network. Pull a short, project-scoped brief instead of re-reading transcripts,
and search prior work semantically. All commands go through the bundled
`memory.py` (stdlib only).

## Which hub: run `status` first

There is **no single fixed hub**. The owner's PC dual-boots: the hub usually
runs on Pop!_OS (`pop-os`) and, when Windows is booted, on `rxla-pc-1`. Every
command probes the candidates in order (`AGENT_MEMORY_URL`, comma-separated
allowed; then `hubs` in `~/.agent-memory/config.json`; then those two
defaults) and uses the first one whose `/health` answers.

```bash
python3 memory.py status        # hubs tried + errors, selected hub, watcher, spool backlog
```

- Use whatever hub `status` selects; don't hard-code or "fix" a URL yourself.
- If it says **NONE REACHABLE** (exit code 3): on a machine with the hub code,
  `memory.py --start-local status` starts a hub on this device's Tailscale
  address. Otherwise **ask the user which hub to use** (then pass `--hub URL`).
  Don't guess, and don't drop the work: the watcher keeps spooling sessions
  until a hub answers.
- If `status` flags a *fallback* hub, sessions written there are missing from
  the preferred hub. Once both are up:
  `python3 memory.py sync --from <fallback> --to <preferred>` (idempotent, safe
  to re-run; `--dry-run` to preview).

Invoke `memory.py` by absolute path from the project directory, or put
`--cwd <project-path>` BEFORE the subcommand. Scope comes from that project's
git remote, so running from the skill directory without `--cwd` gives the wrong
project.

## At the start of a coding session

```bash
python3 memory.py --cwd <project-path> brief
```

The brief covers only THIS project: recent session summaries, decisions in
effect and open threads, usually a few hundred tokens. **It only shows
summarized sessions**, and the hub writes a session's summary after it has
been idle for 30 minutes, so very recent work may show up only in `sessions`
or `search`.

**"No prior sessions recorded" does not mean the work is new.** The project key
is a hard partition: the same work from another checkout, parent directory or
machine lands under a different key. The brief then lists **Related projects
with recorded sessions**; check them before concluding there is no history:

```bash
python3 memory.py --project-key "<key it suggested>" brief
python3 memory.py global-search "<the thing you're looking for>"
```

## During the task

```bash
python3 memory.py search "why did we drop the shannon runtime"   # this project
python3 memory.py threads                                        # unfinished work
python3 memory.py sessions                                       # recent sessions, incl. unsummarized
python3 memory.py global-search "nvidia nim rate limit"          # ALL projects
```

If search returns "No relevant prior context", trust it. Widen only
deliberately with `global-search` or `--scope linked`.

## When you make a decision worth keeping

```bash
python3 memory.py decide "Use sqlite-vec, not Mongo" \
  "Self-hosted Mongo has no vector search." \
  --rationale "Keeps everything in one file on the tailnet."
```

Pin choices future sessions must respect. Routine activity is captured by the
watcher; `decide` records a decision, not a session upload.

## How sessions get in

- The per-device **watcher** tails Claude Code and Codex transcripts and the
  Hermes session store (`state.db`, via the watcher's Hermes adapter), redacts
  secrets locally, spools, and delivers to the first reachable hub.
- On Windows it runs as the **AgentMemoryWatcher** Scheduled Task, installed
  with `powershell -ExecutionPolicy Bypass -File scripts\install_watcher_windows.ps1`
  (add `-Uninstall` to remove). Log: `~/.cache/agent-memory/watcher.log`.
- Device names: `RXLA_PC_1` (Windows) and `RXLA_PC_1-wsl` (WSL).
- `status` shows whether the watcher is running, which hub it last delivered
  to, and the spool backlog. Verify ingestion (`sessions`) separately before
  reporting session sync as working.

## Configuration and secrets

`AGENT_MEMORY_URL` is a **user** environment variable. The shared key, if one
is ever set, goes only in the `AGENT_MEMORY_SHARED_KEY` user environment
variable, never in a file (config.json is never read for keys). Never store
secrets, tokens or credentials in memory, and never your hidden
chain-of-thought.

## MCP alternative

If the `agent-memory` MCP server is configured, the same operations are tools
(`get_project_brief`, `search_memory`, `record_decision`, `list_sessions`,
`open_threads`); it resolves the hub the same way. The CLI always works.
