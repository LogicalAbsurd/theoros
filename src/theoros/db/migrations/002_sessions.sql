-- =============================================================================
-- Migration 002: sessions
-- Date:          2026-04-21
-- Phase:         1 (Schemas)
--
-- Creates the sessions table and retrofits raw_events.session_id as a real
-- foreign key pointing at it.
--
-- A session is a coherent activity block derived from the raw event stream
-- by a normalization pass (idle-threshold + dominance-threshold heuristics,
-- defined later). Sessions are assigned to raw_events retroactively — the
-- capture service writes session_id = NULL, and a periodic job backfills it
-- once session boundaries are detected. Raw events never move between
-- sessions destructively: when the detection algorithm is revised, a new
-- batch of sessions is written under a new detection_algorithm_version and
-- raw_events.session_id is repointed. Old session rows can be kept or
-- purged at the operator's discretion.
--
-- SQLite doesn't permit adding a FOREIGN KEY to an existing column via
-- ALTER TABLE, so the second half of this migration uses the standard
-- 12-step recreate pattern documented at https://sqlite.org/lang_altertable.html:
-- build a new raw_events with the FK, copy rows across, drop the old,
-- rename the new into place, and rebuild the indexes.
-- =============================================================================

-- Foreign keys must be OFF during the table rebuild. With it ON, the DROP
-- of the old raw_events would fire any cascading rules (none yet, but the
-- pragma recommendation is unconditional for schema migrations). PRAGMA is
-- not transactional, so it has to be set before BEGIN.
PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- -----------------------------------------------------------------------------
-- Part 1: create the sessions table.
-- -----------------------------------------------------------------------------
CREATE TABLE sessions (
    -- Surrogate key. AUTOINCREMENT for the same reason raw_events uses it:
    -- monotonic, never-reused IDs so downstream summaries and reflections
    -- that cite a session_id stay stable even if sessions are deleted.
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ISO-8601 UTC timestamp of the first raw event in this session.
    -- Derived during detection from MIN(raw_events.captured_at) over the
    -- member rows. Drives time-range queries (give me sessions from today,
    -- from this week, etc.).
    started_at TEXT NOT NULL,

    -- ISO-8601 UTC timestamp of the last raw event in this session.
    -- Derived from MAX(raw_events.captured_at). A session's duration is
    -- (ended_at - started_at); we store both endpoints rather than a
    -- duration column so re-querying against a revised event set stays
    -- cheap.
    ended_at TEXT NOT NULL,

    -- How many raw_events belong to this session. Denormalized so the
    -- reading UI and reflection pipeline can rank/filter sessions by size
    -- without a COUNT(*) join back to raw_events every time.
    event_count INTEGER NOT NULL,

    -- The source_app most events in this session came from (simple modal
    -- value; ties broken arbitrarily by the detector). Nullable because a
    -- session may genuinely have no dominant app — short sessions, or
    -- sessions assembled entirely from OCR rows where source_app was
    -- never identified.
    dominant_source_app TEXT,

    -- Local-LLM-generated session summary, markdown. NULL at session-
    -- creation time; populated by a later distillation pass (Phase 3).
    -- Stored here rather than in a separate table because one session has
    -- exactly one summary and the 1:1 join would buy nothing.
    summary TEXT,

    -- Which version of the session-detection algorithm produced this row.
    -- The detector is expected to evolve (v1 = naive idle/dominance
    -- thresholds; later versions may use embeddings, topic continuity,
    -- etc.). Recording the version lets us re-run detection with a new
    -- algorithm, write a fresh batch of sessions, and know which rows
    -- came from which pass without ambiguity.
    detection_algorithm_version TEXT NOT NULL,

    -- ISO-8601 UTC timestamp of when this session row was written. Distinct
    -- from started_at/ended_at (which describe the events) and useful for
    -- auditing when detection last ran.
    created_at TEXT NOT NULL
);

-- Sessions are queried by time range constantly: daily summaries pull
-- sessions that ended today, weekly reflections pull sessions that started
-- this week, the reading UI paginates by started_at DESC. Index matches.
CREATE INDEX idx_sessions_started_at ON sessions(started_at);

-- -----------------------------------------------------------------------------
-- Part 2: retrofit raw_events.session_id as a real FK.
--
-- The column already exists from migration 001 as a plain INTEGER. We
-- rebuild the table so the constraint is declared inline, since SQLite
-- has no ALTER TABLE ... ADD CONSTRAINT.
-- -----------------------------------------------------------------------------

-- New table identical to migration 001's raw_events, with the single
-- addition of the FK on session_id. ON DELETE SET NULL: if a session row
-- is removed (e.g. after a re-detection pass), the raw events survive
-- unattached rather than being deleted with it — the whole point of the
-- append-only log is that raw events outlive any interpretive layer.
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
    confidence REAL,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    redactions_applied TEXT DEFAULT '[]',
    raw_metadata TEXT
);

-- Copy every row across. Column list is explicit on both sides so a future
-- schema drift between migration 001 and this file would fail loudly
-- rather than silently misaligning.
INSERT INTO raw_events_new (
    id, captured_at, received_at, source_tier, source_app, source_identifier,
    title, content_text, content_length, content_hash, confidence,
    session_id, redactions_applied, raw_metadata
)
SELECT
    id, captured_at, received_at, source_tier, source_app, source_identifier,
    title, content_text, content_length, content_hash, confidence,
    session_id, redactions_applied, raw_metadata
FROM raw_events;

-- Drop the old table. Its indexes (idx_events_captured_at,
-- idx_events_content_hash) are dropped with it automatically.
DROP TABLE raw_events;

-- Rename new into place. After this, the table is back under its canonical
-- name and downstream code references need no change.
ALTER TABLE raw_events_new RENAME TO raw_events;

-- Recreate the indexes from migration 001 on the rebuilt table. Kept
-- identical in name and definition so this migration is the only place
-- a reader needs to look to understand the retrofit.
CREATE INDEX idx_events_captured_at ON raw_events(captured_at);
CREATE INDEX idx_events_content_hash ON raw_events(content_hash);

-- Sanity-check the FK graph before committing. If any raw_events.session_id
-- points at a nonexistent sessions.id, this raises and the transaction
-- rolls back. At migration time this should always pass because no
-- session rows exist yet and every session_id is currently NULL, but the
-- check is cheap insurance.
PRAGMA foreign_key_check;

COMMIT;

-- Re-enable FK enforcement for subsequent connections. Again, PRAGMA is
-- not transactional, so this lives outside the COMMIT.
PRAGMA foreign_keys = ON;
