-- =============================================================================
-- Migration 003: normalized_records
-- Date:          2026-04-21
-- Phase:         1 (Schemas)
--
-- Creates the normalized_records table: the distilled, deduped output of
-- the normalization pass that reads raw_events.
--
-- One row per coherent piece of content — a webpage, a chat thread, a
-- document, a focused note session. When the same content is captured by
-- multiple tiers (e.g. a long-form article seen by both the browser
-- extension and the OCR catchall), the normalizer collapses them into a
-- single normalized row and records all contributing raw_events.id values
-- in source_event_ids.
--
-- What this table is NOT:
--   - It is not the chunking layer. Chunking for embeddings happens at
--     embedding-generation time (Phase 4+), downstream of this table.
--     A single long normalized record can yield many embedding chunks.
--   - It is not a cache of raw_events. Rebuilding it from raw_events must
--     always be possible; content_hash makes re-runs idempotent.
--
-- Relationship to sessions: session_id is set during normalization by
-- matching the record's originating events to the session they fall into.
-- ON DELETE SET NULL mirrors raw_events — sessions are interpretive, the
-- normalized record survives session churn.
-- =============================================================================

CREATE TABLE normalized_records (
    -- Surrogate key. AUTOINCREMENT for the same reason the other tables
    -- use it: monotonic, never-reused IDs so downstream references
    -- (embeddings, summaries, reflections) stay stable across deletes.
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ISO-8601 UTC timestamp of when this normalized row was written.
    -- Distinct from the underlying raw_events' captured_at — this is a
    -- normalization-pipeline clock, useful for answering "what did the
    -- normalizer produce today" and for auditing pipeline backlog.
    created_at TEXT NOT NULL,

    -- JSON array of raw_events.id values this record was derived from,
    -- e.g. '[4821, 4835, 5102]'. Typically one element, but multiple when
    -- cross-tier dedup merged several captures. Stored as JSON rather
    -- than as a join table because the cardinality is small (usually
    -- single-digit), reads are always "give me the full set," and a
    -- side table would buy nothing except extra joins. A future migration
    -- can extract this into normalized_record_sources if the query
    -- pattern ever changes.
    source_event_ids TEXT NOT NULL,

    -- Which session this record belongs to. Nullable so the normalizer
    -- can write a record before session-detection has caught up. FK set
    -- to NULL on session deletion for the same reason raw_events uses
    -- SET NULL: the normalized record outlives any interpretive layer.
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,

    -- Cleaned, human-readable title (page title without site suffix,
    -- document name, chat-thread subject). Nullable because not every
    -- captured fragment has a meaningful title — an OCR sweep of a
    -- terminal may have none.
    canonical_title TEXT,

    -- The URL if applicable (browser-tier content, Obsidian URIs, etc.).
    -- Nullable for non-web content. "Canonical" means post-cleaning:
    -- tracking parameters stripped, session tokens removed, trailing
    -- slashes normalized — so the same article seen via different
    -- share links collapses to one URL.
    canonical_url TEXT,

    -- Normalized app identifier. Same value space as raw_events.source_app
    -- but after normalization — e.g. 'firefox' rather than whatever the
    -- raw capture carried. Nullable when the source was OCR without a
    -- reliably identified foreground app.
    source_app TEXT,

    -- The deduped, cleaned text. NOT NULL because a normalized row with
    -- no content has no reason to exist. This is the text that Phase 3
    -- distillation and Phase 4 retrieval operate on.
    content_text TEXT NOT NULL,

    -- Character count of content_text. Denormalized for the same reason
    -- as raw_events.content_length — filtering and ranking without
    -- scanning the full text column.
    content_length INTEGER NOT NULL,

    -- SHA-256 hex of content_text. Used for idempotency on re-runs: if
    -- the normalizer is re-run over the same raw_events span, records
    -- with a matching content_hash can be skipped rather than duplicated.
    -- Also enables cross-record dedup if two different event clusters
    -- happen to yield identical text.
    content_hash TEXT NOT NULL,

    -- JSON array of topic tags from the local LLM, e.g.
    -- '["alchemy", "project-theoros", "privacy"]'. Nullable because
    -- topic extraction may lag behind normalization — a record can exist
    -- in the table before topics are assigned. Populating happens during
    -- the same normalization pass for v1, but keeping it nullable leaves
    -- room for an asynchronous tagging stage later.
    topics TEXT,

    -- JSON array of named entities from the local LLM, e.g. people,
    -- projects, places, works referenced. Same nullability reasoning as
    -- topics. Schema intentionally loose at this stage — entity
    -- structure (type, confidence, span offsets) can graduate into its
    -- own table if retrieval ever needs it.
    entities TEXT,

    -- 0.0-1.0 relevance score from the local LLM. The reflection
    -- pipeline will filter low-relevance records out of retrieval so
    -- trivial captures (cookie banners, navigation chrome, OCR noise)
    -- don't dilute the signal. Nullable because scoring may be deferred
    -- or unavailable; consumers treat NULL as "unscored, include by
    -- default" rather than as a zero.
    relevance_score REAL,

    -- Which version of the normalization pipeline produced this row.
    -- Same reasoning as sessions.detection_algorithm_version: the
    -- normalizer will evolve (v1 = basic dedup + LLM tagging; later
    -- versions may add cross-session entity linking, improved cleaning,
    -- etc.). Versioning lets us re-run normalization and know which
    -- rows came from which pass.
    normalization_algorithm_version TEXT NOT NULL
);

-- Time-range queries drive the reading UI and the reflection pipeline:
-- "what did the normalizer produce today / this week." Index on
-- created_at rather than on the underlying events' timestamps because
-- this column is the pipeline-facing axis.
CREATE INDEX idx_norm_created_at ON normalized_records(created_at);

-- Session-scoped retrieval is the hot path for session summaries: "give
-- me every normalized record in session 4821." Without this index that
-- query would scan the entire table on every summary pass.
CREATE INDEX idx_norm_session_id ON normalized_records(session_id);

-- Idempotency checks on re-normalization joins on content_hash. Also
-- enables cross-record dedup queries.
CREATE INDEX idx_norm_content_hash ON normalized_records(content_hash);
