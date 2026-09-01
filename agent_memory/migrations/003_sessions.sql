-- Session model: projects, sessions, raw events, and generated summaries.

CREATE TABLE IF NOT EXISTS projects (
    project_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    repo_root TEXT,
    git_remote TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Opt-in cross-project recall. Absent a row here, projects cannot see
-- each other except via an explicit scope="global" request.
CREATE TABLE IF NOT EXISTS project_links (
    project_key TEXT NOT NULL,
    linked_project_key TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_key, linked_project_key)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    agent_id TEXT,
    device_name TEXT,
    tool TEXT NOT NULL DEFAULT 'unknown',
    cwd TEXT,
    git_branch TEXT,
    ai_title TEXT,
    last_prompt TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    last_event_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    summary_dirty INTEGER NOT NULL DEFAULT 1,
    summarized_event_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_key, last_event_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, last_event_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_dirty ON sessions(summary_dirty, last_event_at);

-- Append-only raw log. High volume; deliberately NOT vector indexed.
-- Retrieval happens over summaries and promoted high-signal events.
CREATE TABLE IF NOT EXISTS session_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    files_json TEXT NOT NULL DEFAULT '[]',
    commands_json TEXT NOT NULL DEFAULT '[]',
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

-- Idempotent ingest: the watcher can safely replay a spool after reconnect.
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_events_seq ON session_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_session_events_session_ts ON session_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_session_events_kind ON session_events(session_id, kind);

CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT NOT NULL,
    tier TEXT NOT NULL,
    summary TEXT NOT NULL,
    decisions_json TEXT NOT NULL DEFAULT '[]',
    open_threads_json TEXT NOT NULL DEFAULT '[]',
    files_json TEXT NOT NULL DEFAULT '[]',
    model TEXT,
    event_count INTEGER NOT NULL DEFAULT 0,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, tier),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
