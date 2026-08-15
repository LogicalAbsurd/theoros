-- =============================================================================
-- Migration 001: raw_events
-- Date:          2026-04-21
-- Phase:         1 (Schemas)
--
-- Creates the append-only raw event log. This is the firehose: every capture
-- event from every tier (browser extension, AT-SPI daemon, OCR catchall)
-- lands here first, before any normalization or deduplication. Rows are
-- written by the local capture service and are never updated or deleted in
-- the normal course of operation — they are the source of truth from which
-- every downstream record (normalized captures, session summaries, daily
-- reflections) can be rederived. Operator-initiated purge is the only path
-- that removes rows.
-- =============================================================================

CREATE TABLE raw_events (
    -- Surrogate key. AUTOINCREMENT (not just INTEGER PRIMARY KEY) guarantees
    -- monotonic, never-reused IDs even across deletes, which matters for a
    -- log whose ordering is referenced by downstream records.
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- When the capture event actually occurred on the operator's machine.
    -- ISO-8601 UTC string, e.g. '2026-04-21T14:32:17.123Z'. Stored as TEXT
    -- rather than a numeric epoch so rows stay human-readable from sqlite3
    -- shell during debugging; ISO-8601 sorts lexicographically in UTC.
    captured_at TEXT NOT NULL,

    -- When the capture service wrote the row. Usually within milliseconds
    -- of captured_at, but can drift — OCR batches, queued browser events
    -- replayed after a service restart. Keeping both lets us audit capture
    -- latency and spot tiers falling behind.
    received_at TEXT NOT NULL,

    -- Which capture tier produced this row. Enforced via CHECK so a typo
    -- in a new capture client can't quietly pollute the log with an
    -- unrecognized tier string.
    source_tier TEXT NOT NULL CHECK (source_tier IN ('browser', 'atspi', 'ocr')),

    -- The application the capture came from ('firefox', 'chrome',
    -- 'obsidian', 'konsole', etc.). Nullable because OCR cannot always
    -- reliably identify the foreground app from a screenshot alone.
    source_app TEXT,

    -- Tier-specific location handle: URL for browser, window title or
    -- AT-SPI widget path for atspi, window title for ocr. Kept as a single
    -- TEXT column rather than separate url/path columns because "where did
    -- this come from" is a tier-defined concept, not a universal one.
    source_identifier TEXT,

    -- Human-readable label — page title, window title, document name.
    -- Used for debugging and for surfacing raw events in the reading UI.
    title TEXT,

    -- The actual captured text. Can be large: long articles, full Obsidian
    -- notes, dense OCR dumps. SQLite TEXT has no practical size limit short
    -- of the per-row cap, so no chunking happens here — chunking is a
    -- normalization-stage concern.
    content_text TEXT,

    -- Character count of content_text. Denormalized so we can filter out
    -- trivially-short events (empty captures, single-keystroke leakage)
    -- without scanning the full text column.
    content_length INTEGER NOT NULL,

    -- SHA-256 of content_text as a lowercase hex string. Used for cross-tier
    -- dedup during normalization (the same paragraph captured by both
    -- browser and OCR should collapse to one normalized record) and for
    -- integrity checks during backup/restore.
    content_hash TEXT NOT NULL,

    -- 0.0-1.0 confidence score. Meaningful mainly for OCR, where it reflects
    -- tesseract or vision-model confidence. Nullable because browser and
    -- AT-SPI captures are structurally accurate and don't carry a score.
    confidence REAL,

    -- Nullable foreign key to the sessions table (added in a later
    -- migration once session-boundary detection is implemented). Populated
    -- by the normalization stage, not by the capture service. Declared
    -- here as a plain INTEGER; the FK constraint will be added when the
    -- sessions table exists rather than left as a forward reference.
    session_id INTEGER,

    -- JSON array of redaction categories applied to content_text before
    -- this row was written, e.g. '["api_key", "credit_card"]'. The
    -- credential-pattern redactor runs pre-insert on all three tiers, so
    -- '[]' means "scanned, nothing matched" — meaningfully distinct from
    -- NULL, which would mean "never scanned." Default '[]' enforces that.
    redactions_applied TEXT DEFAULT '[]',

    -- JSON blob for tier-specific extras: DOM path, AT-SPI role hierarchy,
    -- OCR bounding-box data, browser tab ID — anything a tier wants to
    -- preserve without earning its own column. Keeps the schema stable as
    -- tiers evolve.
    raw_metadata TEXT
);

-- Time-range queries drive nearly all downstream work (session detection,
-- daily/weekly reflection windows, operator-initiated purge). Index on
-- captured_at rather than received_at since the former is the meaningful
-- axis for every consumer.
CREATE INDEX idx_events_captured_at ON raw_events(captured_at);

-- Cross-tier dedup during normalization joins on content_hash; without this
-- index the browser/OCR merge would table-scan the firehose on every run.
CREATE INDEX idx_events_content_hash ON raw_events(content_hash);
