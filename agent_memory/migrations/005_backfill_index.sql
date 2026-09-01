-- Adopt pre-existing databases: fold legacy entry chunks into the unified
-- index, then retire the old per-entry FTS table. All of its data is derivable
-- from entry_chunks, and leaving a second, no-longer-maintained index in place
-- would silently diverge from memory_chunks.

INSERT OR IGNORE INTO memory_chunks (
    chunk_id, source_type, source_ref, project_key, namespace, agent_id,
    kind, title, content, tags, visibility, recipient_id, chunk_index, ts
)
SELECT
    c.chunk_id,
    'entry',
    e.entry_id,
    '__global__',
    e.namespace,
    e.agent_id,
    e.kind,
    e.title,
    c.content,
    trim(replace(replace(replace(replace(e.tags_json, '[', ''), ']', ''), '"', ''), ',', ' ')),
    e.visibility,
    e.recipient_id,
    c.chunk_index,
    e.updated_at
FROM entry_chunks c
JOIN entries e ON e.entry_id = c.entry_id;

INSERT INTO memory_chunks_fts (chunk_id, title, tags, content)
SELECT chunk_id, title, tags, content FROM memory_chunks
WHERE source_type = 'entry'
  AND chunk_id NOT IN (SELECT chunk_id FROM memory_chunks_fts);

DROP TABLE IF EXISTS entry_chunks_fts;
