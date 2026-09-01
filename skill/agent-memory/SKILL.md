---
name: agent-memory
description: >-
  Shared, project-scoped memory across all your Claude and Codex sessions and
  devices. Use at the START of any coding session to load a compact brief of
  what prior sessions did in THIS project, and mid-task to search past work by
  meaning instead of re-reading transcripts. Triggers whenever you are picking
  up existing work, wondering "have we done this before / why did we choose X",
  handing off between agents, or want to avoid re-deriving context you already
  established elsewhere.
---

# Shared Agent Memory

One memory store, shared by every Claude/Codex session on your Tailscale
network. It fixes the expensive habit of re-reading a whole chat to recover
context: instead you pull a short, project-scoped brief and search prior work
semantically. It also lets independent agents on different devices see each
other's progress on the same project.

## The one rule that saves the most tokens

**At the start of a coding session, call `get_project_brief` once, passing the
current working directory as `cwd`.** You get back — scoped to *this* project
only — recent session summaries, open threads, and locked-in decisions, for a
few hundred tokens. That is almost always cheaper and more accurate than
reading files or scrollback to reconstruct where things stand.

```
get_project_brief(cwd="/home/you/project")
```

If the brief says there is no prior context, start fresh — do not invent a
backstory.

## During the task

- **`search_memory(query, cwd=...)`** — semantic + keyword search, automatically
  scoped to the current project so unrelated work never pollutes results. Use it
  for "have we hit this before", "why did we choose X", "where is Y configured".
  If it returns *no relevant context*, trust that and proceed — a forced weak
  match is what sends you off track.
- **`open_threads(cwd=...)`** — the unfinished work items across this project's
  sessions.
- **`list_sessions(cwd=...)` / `get_session(session_id)`** — see what other
  sessions (yours or another agent's) did; fetch one in `summary` or `full`
  detail.

## What to write back

- **`record_decision(...)`** — when you make a choice future work must respect
  (a library, an architecture, a "we tried X and it failed"), pin it with its
  rationale. This is what stops the next agent re-litigating settled questions.
- **`publish_memory(...)`** — durable facts worth keeping: paths, commands,
  hashes, ports, repro steps, environment quirks. Dense and specific.

Most session context is captured automatically by the background watcher, so
you rarely need to log routine activity by hand — reserve explicit writes for
conclusions and decisions.

## Scope: how unrelated work is kept out

Every memory belongs to a `project_key` derived from the git remote (or path)
of its `cwd`. Searches and briefs are confined to the current project by
default. To deliberately look wider:

- `scope="linked"` — also read projects you explicitly linked.
- `scope="global"` — read everything (use sparingly; this is how cross-project
  noise gets in).

## Never store

Secrets, API keys, tokens, or raw credentials — and never your hidden
chain-of-thought. The watcher redacts known secret shapes on the source device,
but do not rely on it: keep secrets out of what you write.
