-- Every listing query orders by updated_at; it had no index.
CREATE INDEX IF NOT EXISTS idx_entries_updated_at ON entries(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_agent ON entries(agent_id);
-- Messages inbox filters on kind + recipient together.
CREATE INDEX IF NOT EXISTS idx_entries_kind_recipient ON entries(kind, recipient_id, updated_at DESC);
