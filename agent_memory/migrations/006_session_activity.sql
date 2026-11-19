-- last_event_at means "when the work happened": the newest event timestamp
-- from the transcript. It used to be stamped with upload time when a session
-- row was created, so a backfill of months-old transcripts made every one of
-- them look active today and pushed real recent work out of the brief, the
-- sessions list and the dashboard. Upload time now has its own column.

ALTER TABLE sessions ADD COLUMN last_ingest_at TEXT;

UPDATE sessions SET last_ingest_at = last_event_at;

UPDATE sessions SET last_event_at = (
    SELECT MAX(ts) FROM session_events WHERE session_events.session_id = sessions.session_id
)
WHERE EXISTS (SELECT 1 FROM session_events WHERE session_events.session_id = sessions.session_id);
