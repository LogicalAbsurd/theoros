-- =============================================================================
-- Migration 004: chain_columns
-- Date:          2026-04-26
-- Phase:         2 (Capture — mutating-page redesign)
--
-- Adds delta-chain support to raw_events so mutating pages (long-lived tabs
-- whose content changes over time) are captured as continuation chains rather
-- than as duplicate full-text snapshots.
--
-- A chain is a sequence of raw_events rows sharing the same chain_id. The
-- first row (chain_position = 1, is_chain_root = 1) carries the full initial
-- text in content_text. Subsequent rows (chain_position 2, 3, ...) carry only
-- the new content contributed by that capture in delta_text, with content_text
-- set to NULL. This avoids storing the full page text on every capture cycle
-- for tabs that add a few paragraphs between visits.
--
-- Semantics for consumers:
--   - content_text retains its existing meaning: full text of the captured
--     surface at that moment. NULL on continuation rows.
--   - delta_text is the new content this row contributes. On a chain root,
--     delta_text equals content_text (or is NULL — both forms are valid;
--     consumers must treat them as equivalent). On a continuation row,
--     delta_text is the suffix that was not present in the previous row.
--   - To reconstruct full content at position N: walk the chain from
--     position 1 and concatenate delta_text values. Shortcut: chain root's
--     content_text + concatenation of delta_text from positions 2 through N.
--
-- Backfill: existing rows (the captures already in the table) each become a
-- chain of length 1 — chain_id gets a fresh UUID, chain_position = 1,
-- is_chain_root = 1, delta_text = NULL (content_text already holds the full
-- text, so the NULL-on-root form applies).
--
-- Uses the SQLite 12-step recreate pattern (same as migration 002) because
-- we need computed defaults across existing rows and SQLite's ALTER TABLE
-- ADD COLUMN can't do that.
-- =============================================================================

-- Foreign keys OFF before the rebuild. PRAGMA is not transactional, so it
-- must precede BEGIN.
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- New table: raw_events with the four chain columns added after content_hash
-- (grouping chain columns together keeps the schema readable).
CREATE TABLE raw_events_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_tier TEXT NOT NULL CHECK (source_tier IN ('browser', 'atspi', 'ocr')),
    source_app TEXT,
    source_identifier TEXT,
    title TEXT,
    content_text TEXT,
    content_length INTEGER NOT NULL,
    content_hash TEXT NOT NULL,

    -- UUID (v4, stored as lowercase hex text) identifying which chain this
    -- row belongs to. Every row has one — single-shot captures are chains of
    -- length 1. NULL is structurally allowed for future cases we haven't
    -- anticipated, but in normal operation every row will carry a chain_id.
    chain_id TEXT,

    -- 1-indexed position within the chain. Position 1 is the chain root
    -- (the initial full-text capture). Subsequent positions carry only the
    -- delta. Together with chain_id, this column uniquely identifies a row's
    -- place in the chain.
    chain_position INTEGER,

    -- Denormalized boolean (SQLite integer, 0 or 1): 1 if chain_position = 1,
    -- 0 otherwise. Stored explicitly so "give me all chain roots in this time
    -- range" can use a partial index and skip scanning continuation rows.
    is_chain_root INTEGER,

    -- The new content contributed by this row. On a chain root, equals
    -- content_text or is NULL (both valid; consumers treat them as
    -- equivalent). On a continuation row, this is the suffix that was not
    -- present in the previous row's accumulated text.
    delta_text TEXT,

    confidence REAL,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    redactions_applied TEXT DEFAULT '[]',
    raw_metadata TEXT
);

-- Copy existing rows, backfilling chain columns. Each row becomes a chain
-- of length 1: fresh UUID, position 1, is_chain_root = 1, delta_text = NULL
-- (the NULL-on-root form, since content_text already holds the full text).
--
-- UUID generation uses stdlib-only randomblob() to produce a v4-like UUID.
-- Verbose but no extension required.
INSERT INTO raw_events_new (
    id, captured_at, received_at, source_tier, source_app, source_identifier,
    title, content_text, content_length, content_hash,
    chain_id, chain_position, is_chain_root, delta_text,
    confidence, session_id, redactions_applied, raw_metadata
)
SELECT
    id, captured_at, received_at, source_tier, source_app, source_identifier,
    title, content_text, content_length, content_hash,
    -- v4 UUID: 8-4-4-4-12 hex with version nibble = 4 and variant bits in
    -- the correct position. One fresh UUID per row.
    lower(hex(randomblob(4))) || '-' ||
    lower(hex(randomblob(2))) || '-4' ||
    substr(lower(hex(randomblob(2))), 2) || '-' ||
    substr('89ab', abs(random()) % 4 + 1, 1) ||
    substr(lower(hex(randomblob(2))), 2) || '-' ||
    lower(hex(randomblob(6)))
    AS chain_id,
    1 AS chain_position,
    1 AS is_chain_root,
    NULL AS delta_text,
    confidence, session_id, redactions_applied, raw_metadata
FROM raw_events;

-- Drop the old table. Its indexes (idx_events_captured_at,
-- idx_events_content_hash) are dropped with it automatically.
DROP TABLE raw_events;

-- Rename new into place.
ALTER TABLE raw_events_new RENAME TO raw_events;

-- Recreate existing indexes from prior migrations.
CREATE INDEX idx_events_captured_at ON raw_events(captured_at);
CREATE INDEX idx_events_content_hash ON raw_events(content_hash);

-- New index: chain reconstruction queries look up all rows sharing a
-- chain_id and order by chain_position. Index on chain_id makes the
-- lookup O(log n) rather than a table scan.
CREATE INDEX idx_events_chain_id ON raw_events(chain_id);

-- New partial index: "show me all distinct chains in the last hour" and
-- similar time-range queries only need chain roots, not continuations.
-- The WHERE clause excludes continuation rows from the index entirely,
-- keeping it small and fast.
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
