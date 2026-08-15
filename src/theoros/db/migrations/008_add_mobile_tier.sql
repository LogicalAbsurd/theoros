-- =============================================================================
-- Migration 008: add mobile source tier
-- Date:          2026-07-16
-- Phase:         2 (capture tiers)
--
-- Widens the source_tier CHECK constraint on raw_events to include
-- 'mobile', a fifth capture tier for events originating on the operator's
-- phone and posted to the capture service through the Cloudflare
-- tunnel (same auth posture as /exclusions/urls: require_cf_access
-- for remote clients, localhost bypass for the browser extension).
--
-- Uses the SQLite 12-step recreate pattern (same as migrations 002,
-- 004, and 006) because CHECK constraints cannot be altered in place.
-- =============================================================================

-- Foreign keys OFF before the rebuild. PRAGMA is not transactional, so
-- it must precede BEGIN.
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- New table: identical schema to current raw_events (through migration
-- 006), with the CHECK constraint widened to include 'mobile'.
CREATE TABLE raw_events_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_tier TEXT NOT NULL CHECK (source_tier IN ('browser', 'atspi', 'ocr', 'window_meta', 'mobile')),
    source_app TEXT,
    source_identifier TEXT,
    title TEXT,
    content_text TEXT,
    content_length INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    chain_id TEXT,
    chain_position INTEGER,
    is_chain_root INTEGER,
    delta_text TEXT,
    confidence REAL,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    redactions_applied TEXT DEFAULT '[]',
    raw_metadata TEXT,
    lcp_length INTEGER
);

-- Copy every row across. Column list is explicit on both sides so any
-- schema drift between migrations would fail loudly rather than
-- silently misaligning.
INSERT INTO raw_events_new (
    id, captured_at, received_at, source_tier, source_app, source_identifier,
    title, content_text, content_length, content_hash,
    chain_id, chain_position, is_chain_root, delta_text,
    confidence, session_id, redactions_applied, raw_metadata,
    lcp_length
)
SELECT
    id, captured_at, received_at, source_tier, source_app, source_identifier,
    title, content_text, content_length, content_hash,
    chain_id, chain_position, is_chain_root, delta_text,
    confidence, session_id, redactions_applied, raw_metadata,
    lcp_length
FROM raw_events;

-- Drop the old table. Its indexes are dropped with it automatically.
DROP TABLE raw_events;

-- Rename new into place.
ALTER TABLE raw_events_new RENAME TO raw_events;

-- Recreate all indexes from prior migrations (001, 004).
CREATE INDEX idx_events_captured_at ON raw_events(captured_at);
CREATE INDEX idx_events_content_hash ON raw_events(content_hash);
CREATE INDEX idx_events_chain_id ON raw_events(chain_id);
CREATE INDEX idx_events_chain_root_captured_at
    ON raw_events(is_chain_root, captured_at)
    WHERE is_chain_root = 1;

-- Sanity-check the FK graph. Every session_id should either be NULL or
-- point at a valid sessions.id. Raises on violation, rolling back the
-- transaction.
PRAGMA foreign_key_check;

COMMIT;

-- Re-enable FK enforcement for subsequent connections.
PRAGMA foreign_keys = ON;
