-- Unified retrieval surface. Everything searchable lands here exactly once:
-- explicit entries, generated session summaries, and promoted session events.
-- Keeping one index means one ranking path and no cross-index drift.

CREATE TABLE IF NOT EXISTS memory_chunks (
    chunk_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,          -- entry | session_summary | session_event
    source_ref TEXT NOT NULL,           -- entry_id | session_id | event_id
    project_key TEXT NOT NULL DEFAULT '__global__',
    namespace TEXT NOT NULL DEFAULT 'default',
    agent_id TEXT,
    session_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'shared',
    recipient_id TEXT,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL,
    embed_model TEXT,
    embedded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_chunks_source ON memory_chunks(source_type, source_ref);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_project ON memory_chunks(project_key, ts DESC);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_session ON memory_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_pending ON memory_chunks(embedded_at);

-- Four columns, so bm25() gets exactly four weights and they land where intended.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    title,
    tags,
    content,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS embedding_queue (
    chunk_id TEXT PRIMARY KEY,
    enqueued_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_embedding_queue_next ON embedding_queue(next_attempt_at);
