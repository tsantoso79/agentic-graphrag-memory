-- Add inject_log column to pending_transcripts for exposure scoring
-- Stores which memories were injected at each turn, with content for Sonnet to judge
ALTER TABLE pending_transcripts ADD COLUMN IF NOT EXISTS inject_log JSONB;
