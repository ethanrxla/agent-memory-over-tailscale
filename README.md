# Agent Memory Over Tailscale

A shared, **session-aware RAG memory** for coding agents. It lets multiple
Claude and Codex sessions — across multiple devices, repos, and concurrent
tasks — share one durable memory, so an agent can pick up context in a few
hundred tokens instead of re-reading an entire transcript, and independent
agents can see each other's work.

It solves three problems at once:

- **Agent memory.** Sessions are captured automatically and summarised, so a new
  session starts from a compact brief instead of a cold context window.
- **Relevance isolation.** Everything is scoped to a `project_key`. An agent
  working in one repo never pulls in unrelated context from another.
- **Persistence.** Summaries and decisions live in one SQLite database, so
  context from weeks ago is still retrievable.

## How it works

```
hub (always on, your tailnet)    per-device watcher            per-agent
┌────────────────────────┐       ┌──────────────────┐     ┌──────────────────┐
│ FastAPI  :8787         │◄──────│ tail *.jsonl     │     │ agent-memory     │
│ SQLite + FTS5 + vec0   │ over  │ filter → redact  │     │   skill          │
│  sessions / summaries  │  TS   │ spool (offline)  │     │   ↓ MCP tools    │
│ worker: embed+summarize│       │ POST /v1/sessions│     │ get_project_brief│
└──────────┬─────────────┘       └──────────────────┘     │ search_memory ...│
           └── NVIDIA NIM (optional): embeddings + summaries
```

- **Hub** (`main.py` / `agent_memory/`): FastAPI over SQLite. Keyword search
  (FTS5) and semantic search (`sqlite-vec`, 2048-dim) fused with reciprocal
  rank fusion, scoped by project, gated by a relevance floor.
- **Watcher** (`watcher/`): runs on each device, tails Claude Code and Codex
  transcripts, drops noise (thinking, tool output), **redacts secrets locally**,
  and ships compact events to the hub. Spools while offline, replays on
  reconnect.
- **Background worker**: embeds new chunks and writes rolling + final session
  summaries using a free NVIDIA NIM model. Everything degrades gracefully to
  keyword-only + extractive summaries when no `NVIDIA_API_KEY` is set.
- **Skill + MCP** (`skill/agent-memory/`, `agent_memory_mcp.py`): the tools an
  agent calls — `get_project_brief`, `search_memory`, `record_decision`, etc.

## Hubs and failover

No client hard-codes a hub. `memory.py`, the watcher and the MCP server share
one resolver (`skill/agent-memory/memory.py`, loaded by path elsewhere via
`agent_memory/hubs.py`) that probes candidates in order and uses the first
whose `GET /health` answers within a couple of seconds (cached ~2 min):

1. `AGENT_MEMORY_URL` (user env var; comma-separated list allowed),
2. `hubs` in `~/.agent-memory/config.json`,
3. `DEFAULT_HUBS` in `memory.py`, which ships empty, so a fresh install probes
   only what you configure (`"include_defaults": false` skips it in a private
   copy that fills it in).

List every hub a device should try, preferred first:

```json
{"hubs": ["http://hub.your-tailnet.ts.net:8787", "http://backup.your-tailnet.ts.net:8787"],
 "local_hub": {"repo": "C:/Users/you/Projects/agent-memory-over-tailscale", "port": 8787}}
```

`python3 memory.py status` shows every hub tried (with its error), the
selected one, the watcher's last cycle and the spool backlog — never the key.
The shared key is read only from the `AGENT_MEMORY_SHARED_KEY` env var.

If nothing answers, commands exit with code 3 and the list of hubs tried, and
agents are told to ask the user. On a machine with the hub code,
`memory.py --start-local ...` (or `python scripts/start_local_hub.py`) starts a
hub bound to this device's Tailscale address. The watcher never gives up: it
spools until any configured hub answers, delivers there, and moves back to the
preferred hub as soon as it returns (`--no-fallback` pins one hub).

Two hubs means two databases. Push what landed on the fallback hub to the
preferred one when both are up:

```bash
python3 skill/agent-memory/memory.py sync --from http://backup.your-tailnet.ts.net:8787 \
                                         --to   http://hub.your-tailnet.ts.net:8787 [--since 2026-11-01] [--dry-run]
```

`sync` reads `/v1/export/...` from the source and re-posts through the normal
ingest routes, which are idempotent (events on `(session_id, seq)`, entries on
`(agent_id, namespace, source_id)`), so it is safe to repeat. To replay a
device's transcripts instead, run the watcher with a fresh spool:
`python -m watcher.daemon --backfill --backend-url <hub> --no-fallback --spool /tmp/replay.db`.

## Quick start

Run the hub on one always-on machine on your tailnet (clients fall back to
other configured hubs when it is down):

```bash
docker compose up -d --build
```

The hub listens on `127.0.0.1:8787` only, so put it on your tailnet with
[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve), which
lets only devices on your tailnet reach it:

```bash
tailscale serve --bg --http=8787 http://127.0.0.1:8787
```

The hub is now at its MagicDNS name, `http://hub.your-tailnet.ts.net:8787` with
the hub machine's name in place of `hub` and your tailnet's in place of
`your-tailnet` (`tailscale status --json` on the hub shows it as `Self.DNSName`,
without the `:8787`). Check it from another device with
`curl http://hub.your-tailnet.ts.net:8787/health` and use that address
everywhere below. Give it to the hub as well, so the installer it serves
(`/skill/install.sh`) carries it:

```bash
export AGENT_MEMORY_PUBLIC_URL=http://hub.your-tailnet.ts.net:8787
docker compose up -d
```

Optionally add free NVIDIA NIM credentials for semantic search and prose
summaries (get a key at https://build.nvidia.com — no GPU, no card):

```bash
export NVIDIA_API_KEY=nvapi-...
docker compose up -d
```

Install the watcher on each device:

```bash
./scripts/install_watcher.sh \
  --backend-url http://hub.your-tailnet.ts.net:8787 \
  --shared-key your-shared-key \
  --backfill        # ingest transcripts already on disk, then run as a service
```

Generate MCP client configs:

```bash
./scripts/install.sh \
  --backend-url http://hub.your-tailnet.ts.net:8787 \
  --shared-key your-shared-key \
  --device laptop --device desktop
```

## Using it from an agent

At session start:

```bash
python3 agent_memory_cli.py brief --cwd "$(pwd)"
```

Search prior work, scoped to the current project:

```bash
python3 agent_memory_cli.py search --query "why did we drop the shannon runtime" --cwd "$(pwd)"
```

Pin a decision:

```bash
python3 agent_memory_cli.py decision --agent-id codex-laptop --cwd "$(pwd)" \
  --title "Use sqlite-vec, not Mongo" --content "Self-hosted Mongo has no vector search." \
  --rationale "Keeps everything in one file on the tailnet."
```

Via MCP, agents get these tools: `get_project_brief`, `search_memory`,
`list_sessions`, `get_session`, `record_decision`, `open_threads`, plus the
original `publish_memory`, `send_message`, `read_inbox`, `register_agent`,
`list_agents`, `get_entry`.

## Scope: keeping unrelated work out

Each memory has a `project_key` derived from the git remote (or path) of its
`cwd`. Searches and briefs are confined to the current project by default:

- `scope="project"` (default) — this project only.
- `scope="linked"` — plus explicitly linked projects (`POST /v1/projects/link`).
- `scope="global"` — everything.

Below a tunable relevance floor, a search returns an explicit "no relevant
context" rather than marginal matches — the thing most likely to derail an
agent.

## Storage

One SQLite file (`./data/agent_memory.db`): FTS5 for keyword search, a
`sqlite-vec` `vec0` table for vectors (with `project_key` as a partition key, so
project isolation is enforced by the index itself). No external database. If
`sqlite-vec` is unavailable the hub runs keyword-only.

## Testing

```bash
PYTHONPATH=. python -m pytest -q
```

The dashboard's mute/solo state helpers (`agent_memory/static/project_filter.js`)
are unit-tested under `node` when it is installed (`tests/js/`); those tests
skip otherwise.

## Dashboard

`/` and `/graph` refresh themselves (every 20–30 s, plus a Refresh button) and
are served `Cache-Control: no-store`. Projects are listed by last activity, and
a session's activity time is its newest transcript event, not upload time, so
backfills don't bury current work. In the graph sidebar (which scrolls on its
own), click a project to mute it, or right-click / Shift+F10 / the context-menu
key for **Solo** and **Mute**; the state persists in `localStorage` and is
shared with the dashboard's project list.

## Security

- Bind the hub to a Tailscale-reachable host, not a public interface.
- Set `AGENT_MEMORY_SHARED_KEY` for an application-layer gate on top of
  Tailscale. The dashboard takes the key via header or a `/login` cookie, never
  a query string.
- Secrets are redacted on the source device before anything is sent. Do not
  store credentials or chain-of-thought regardless.
