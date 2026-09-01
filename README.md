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
pop-os (hub, always on)          per-device watcher            per-agent
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

## Quick start

Run the hub on the always-on node (e.g. `pop-os`):

```bash
docker compose up -d --build
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
  --backend-url http://memory-host.tailnet.ts.net:8787 \
  --shared-key your-shared-key \
  --backfill        # ingest transcripts already on disk, then run as a service
```

Generate MCP client configs:

```bash
./scripts/install.sh \
  --backend-url http://memory-host.tailnet.ts.net:8787 \
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

## Security

- Bind the hub to a Tailscale-reachable host, not a public interface.
- Set `AGENT_MEMORY_SHARED_KEY` for an application-layer gate on top of
  Tailscale. The dashboard takes the key via header or a `/login` cookie, never
  a query string.
- Secrets are redacted on the source device before anything is sent. Do not
  store credentials or chain-of-thought regardless.
