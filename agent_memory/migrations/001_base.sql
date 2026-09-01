-- Baseline schema. Reproduces the original inline SCHEMA verbatim so that
-- databases created before the migration runner existed adopt cleanly.

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    device_name TEXT,
    tailscale_name TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    entry_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    recipient_id TEXT,
    visibility TEXT NOT NULL,
    source_uri TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(agent_id) REFERENCES agents(agent_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_agent_namespace_source
ON entries(agent_id, namespace, source_id);

CREATE INDEX IF NOT EXISTS idx_entries_namespace ON entries(namespace);
CREATE INDEX IF NOT EXISTS idx_entries_kind ON entries(kind);
CREATE INDEX IF NOT EXISTS idx_entries_recipient ON entries(recipient_id);

CREATE TABLE IF NOT EXISTS entry_chunks (
    chunk_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    FOREIGN KEY(entry_id) REFERENCES entries(entry_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_entry_chunks_entry ON entry_chunks(entry_id, chunk_index);

CREATE VIRTUAL TABLE IF NOT EXISTS entry_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    entry_id UNINDEXED,
    agent_id,
    namespace,
    kind,
    title,
    tags,
    content,
    tokenize = 'unicode61'
);
