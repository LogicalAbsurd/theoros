-- =============================================================================
-- Supabase Migration 001: initial schema
-- Date:          2026-04-23
-- Phase:         1 (Schemas)
--
-- This file is pasted into the Supabase SQL Editor and run once manually.
-- Every statement is idempotent (IF NOT EXISTS / IF EXISTS) so running it
-- twice is safe.
--
-- Creates the remote-side tables for Theoros. Only distilled outputs live
-- here — session summaries, reflections, observations, themes, and
-- embeddings. Raw captures and normalized records stay in local SQLite.
-- The separation is architectural: local capture can be generous because
-- nothing raw ever leaves the machine; Supabase holds the curated layer
-- that powers phone-readable reflections and semantic retrieval.
--
-- Cross-database references (session_id in session_summaries pointing at
-- sessions.id in local SQLite, source_id_text in embeddings pointing at
-- rows in either database) are documented but cannot be enforced as FKs.
-- Application-layer code is responsible for referential integrity on
-- those columns.
--
-- Design decisions worth reviewing:
--
--   embeddings.source_id_text is TEXT rather than UUID because it must
--   accommodate both UUIDs (Supabase-side tables) and integer IDs stored
--   as text (normalized_records in local SQLite). A polymorphic reference
--   is ugly, but the alternative — separate embedding tables per source —
--   would multiply the vector index count and complicate retrieval queries
--   that search across all content types. One table, one index, one query
--   path. The (source_table, source_id_text, embedding_model) unique
--   constraint prevents accidental double-embedding.
--
--   themes / theme_versions is a two-table pattern (stable identity +
--   append-only versions) rather than a single mutable row. This lets
--   the reflection pipeline reference a theme's content at a specific
--   point in time, avoids lossy overwrites, and keeps a visible edit
--   history. The cost is an extra join; the benefit is auditability and
--   the ability to diff theme evolution.
--
--   ivfflat vs hnsw for the vector index: ivfflat is chosen because it
--   works well at the scale Theoros will operate (tens of thousands of
--   embeddings, not millions) and is simpler to maintain. hnsw would
--   offer better recall at high scale but adds write amplification and
--   memory overhead that aren't justified here. Revisit if the embedding
--   count grows past ~500k.
-- =============================================================================


-- =============================================================================
-- Extensions
-- =============================================================================

-- pgvector: vector column type and similarity operators for embeddings.
CREATE EXTENSION IF NOT EXISTS vector;

-- pgcrypto: gen_random_uuid() for UUID primary key defaults.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- =============================================================================
-- Table: session_summaries
--
-- Local-LLM-generated summaries of activity sessions, promoted to
-- Supabase so they're readable from the phone and available as retrieval
-- context for Claude reflections.
--
-- session_id references sessions.id in local SQLite — an integer, not a
-- UUID. No FK constraint is possible across databases; the promotion
-- pipeline is responsible for writing valid session IDs.
-- =============================================================================

CREATE TABLE IF NOT EXISTS session_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Integer session ID from local SQLite's sessions table. NOT NULL
    -- because a summary without a session is meaningless.
    session_id INTEGER NOT NULL,

    -- Session time boundaries, copied from the local sessions table at
    -- promotion time. Stored here so Supabase queries can filter by time
    -- range without reaching back to the local database.
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,

    -- The summary itself, markdown. Produced by the local LLM during
    -- Phase 3 distillation.
    summary_markdown TEXT NOT NULL,

    -- Local-LLM-extracted topic tags, e.g. ["alchemy", "privacy"].
    -- JSONB array; defaults to empty rather than NULL so consumers can
    -- always iterate without a NULL check.
    topics JSONB DEFAULT '[]'::jsonb,

    -- Local-LLM-extracted proper names, e.g. ["Theoros", "Obsidian"].
    -- Same default reasoning as topics. Renamed from `entities` to match
    -- the local distillation summary key (see commit 419a1ef).
    names JSONB DEFAULT '[]'::jsonb,

    -- Which local LLM model produced this summary. Recorded so we can
    -- assess quality differences if the model changes, and so the
    -- reflection pipeline knows what upstream quality to expect.
    local_llm_model TEXT NOT NULL,

    -- When this row was written to Supabase (promotion time, not
    -- session time).
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_session_summaries_session_id
    ON session_summaries(session_id);

CREATE INDEX IF NOT EXISTS idx_session_summaries_started_at
    ON session_summaries(started_at);


-- =============================================================================
-- Table: reflections
--
-- Claude-generated reflections at daily, weekly, and monthly cadences.
-- Each row is a single reflection produced by one Claude API call,
-- grounded in retrieved evidence (session summaries, prior reflections,
-- theme content).
--
-- The timeframe CHECK constrains to the three defined cadences. On-demand
-- reflective queries (operator asks Theoros a question) are a separate
-- interaction pattern, not stored as reflections — they're more like
-- query/response pairs and will get their own table if needed.
-- =============================================================================

CREATE TABLE IF NOT EXISTS reflections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Which cadence produced this reflection.
    timeframe TEXT NOT NULL CHECK (timeframe IN ('daily', 'weekly', 'monthly')),

    -- The time window this reflection covers. For a daily, period_start
    -- is midnight-to-midnight; for a weekly, Monday-to-Sunday; etc.
    -- Stored as TIMESTAMPTZ so timezone-aware queries work correctly.
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,

    -- The reflection itself, markdown. This is what the operator reads
    -- on the phone.
    body_markdown TEXT NOT NULL,

    -- JSON array of references to the evidence this reflection drew on.
    -- Each element identifies a source table and row, e.g.
    -- {"table": "session_summaries", "id": "abc-123", "excerpt": "..."}.
    -- NOT NULL with a default of '[]' rather than nullable — a reflection
    -- must always declare its evidence, even if the list is empty (which
    -- would itself be a flag that something went wrong in retrieval).
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- Which Claude model produced this reflection. Recorded for the same
    -- reason local_llm_model is recorded on session_summaries: quality
    -- auditing and cost tracking.
    claude_model TEXT NOT NULL,

    -- Version identifier for the prompt template used. Prompt templates
    -- will evolve; recording the version lets us compare output quality
    -- across prompt iterations without ambiguity.
    prompt_template_version TEXT NOT NULL,

    -- Token usage from the Claude API call, e.g.
    -- {"input_tokens": 12345, "output_tokens": 678}. JSONB rather than
    -- separate columns because the shape may vary across model versions.
    -- Default '{}' so it's always valid JSON.
    token_usage JSONB DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Primary query pattern: "give me all weekly reflections" or "give me
-- reflections for this month." Composite index on (timeframe, period_start)
-- covers both.
CREATE INDEX IF NOT EXISTS idx_reflections_timeframe_period
    ON reflections(timeframe, period_start);

-- Secondary: time-range scans across all timeframes (e.g. retrieval
-- context for a monthly reflection needs recent dailies and weeklies).
CREATE INDEX IF NOT EXISTS idx_reflections_period_start
    ON reflections(period_start);


-- =============================================================================
-- Table: observations
--
-- Short, structured, evidence-linked notes. Can be written by the operator
-- directly, generated by the local LLM, or generated by Claude. The
-- observation is the atomic unit of "something noticed" — smaller than
-- a reflection, more pointed than a theme.
--
-- author CHECK constrains to the three valid sources. If a fourth
-- source emerges (e.g. an external integration), the CHECK can be
-- widened in a later migration.
-- =============================================================================

CREATE TABLE IF NOT EXISTS observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Who wrote this observation.
    author TEXT NOT NULL CHECK (author IN ('chris', 'local_llm', 'claude')),

    -- The observation itself, markdown.
    body_markdown TEXT NOT NULL,

    -- Evidence references, same structure as reflections.evidence_refs.
    -- Default '[]' rather than NULL; operator-authored observations may
    -- legitimately have no machine-generated evidence refs, but the
    -- column should still be valid JSON.
    evidence_refs JSONB DEFAULT '[]'::jsonb,

    -- Freeform tags for categorization and retrieval filtering.
    tags JSONB DEFAULT '[]'::jsonb,

    -- When the observation was made (semantic time, not insertion time).
    -- For operator-authored observations this is when the operator wrote it;
    -- for generated ones it's the timestamp of the content that prompted
    -- the observation.
    observed_at TIMESTAMPTZ NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_observations_observed_at
    ON observations(observed_at);

CREATE INDEX IF NOT EXISTS idx_observations_author
    ON observations(author);


-- =============================================================================
-- Table: themes
--
-- Stable identity for a long-lived thematic thread. A theme has a slug
-- (URL-friendly identifier), a title, and a lifecycle (created_at to
-- retired_at). The actual content lives in theme_versions — this table
-- is the anchor.
--
-- retired_at is nullable: NULL means the theme is active. Setting it
-- to a timestamp soft-deletes the theme without destroying its history.
-- Reflections and observations that reference a retired theme keep their
-- references intact.
-- =============================================================================

CREATE TABLE IF NOT EXISTS themes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- URL-friendly slug, e.g. 'alchemy-of-attention'. UNIQUE so themes
    -- can be referenced by slug in CLI commands and URLs without
    -- ambiguity.
    slug TEXT UNIQUE NOT NULL,

    -- Human-readable title, e.g. 'The Alchemy of Attention'.
    title TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- NULL = active. Set to a timestamp to retire the theme.
    retired_at TIMESTAMPTZ
);


-- =============================================================================
-- Table: theme_versions
--
-- Append-only version history for theme content. Each edit (by the operator or
-- by Claude) creates a new version row rather than mutating the theme
-- in place. This preserves the full edit history and lets the reflection
-- pipeline reference theme content at a specific point in time.
--
-- version_number is monotonically increasing per theme. The unique
-- constraint on (theme_id, version_number) prevents accidental
-- collisions; application code is responsible for incrementing correctly.
-- =============================================================================

CREATE TABLE IF NOT EXISTS theme_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Which theme this version belongs to. CASCADE on delete: if a
    -- theme is hard-deleted (not just retired), its version history
    -- goes with it. Hard deletion should be rare — retirement is the
    -- normal lifecycle end.
    theme_id UUID NOT NULL REFERENCES themes(id) ON DELETE CASCADE,

    -- Monotonically increasing version number within this theme.
    -- Starts at 1 for the initial version.
    version_number INTEGER NOT NULL,

    -- The theme content at this version, markdown.
    body_markdown TEXT NOT NULL,

    -- Freeform tags, same purpose as observations.tags.
    tags JSONB DEFAULT '[]'::jsonb,

    -- Optional human-readable note about what changed in this version.
    -- Nullable because the initial version has nothing to summarize.
    edit_summary TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Who authored this version.
    created_by TEXT NOT NULL CHECK (created_by IN ('chris', 'claude')),

    -- No two versions of the same theme share a version number.
    UNIQUE (theme_id, version_number)
);

-- Primary query: "give me the latest version of theme X" — needs
-- theme_id + version_number DESC. Also covers "give me version N of
-- theme X" for the reflection pipeline's point-in-time references.
CREATE INDEX IF NOT EXISTS idx_theme_versions_theme_version
    ON theme_versions(theme_id, version_number DESC);


-- =============================================================================
-- Table: embeddings
--
-- Polymorphic embedding store. One table holds vector embeddings for
-- content from multiple source tables: normalized_records (local
-- SQLite), session_summaries, reflections, observations, and
-- theme_versions (all Supabase). The source_table + source_id_text
-- pair identifies the origin row.
--
-- source_id_text is TEXT rather than UUID because normalized_records
-- uses integer IDs (from local SQLite's AUTOINCREMENT), while all
-- Supabase tables use UUIDs. Storing both as text in a single column
-- is the pragmatic choice — the alternative (separate embedding tables
-- per source, or a UUID-only column with a mapping layer) would
-- complicate retrieval queries that need to search across all content
-- types in one vector scan.
--
-- The unique constraint on (source_table, source_id_text, embedding_model)
-- prevents embedding the same content twice with the same model. If the
-- model changes or content is re-embedded, a new row is created (the
-- old one can be cleaned up by a maintenance job).
-- =============================================================================

CREATE TABLE IF NOT EXISTS embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Which table the embedded content came from.
    source_table TEXT NOT NULL CHECK (source_table IN (
        'normalized_records',
        'session_summaries',
        'reflections',
        'observations',
        'theme_versions'
    )),

    -- Row ID in the source table. UUID string for Supabase tables,
    -- integer-as-text for normalized_records (e.g. '4821').
    source_id_text TEXT NOT NULL,

    -- The embedding vector. 1024 dimensions to match the default output
    -- of voyage-4-large, the embedding model we're using. If we switch
    -- models or need a different dimensionality, that's a schema change
    -- (the vector(1024) column type constrains all vectors to the same
    -- dimensionality, which is required for a single ivfflat index to
    -- work).
    embedding vector(1024) NOT NULL,

    -- Which model produced this embedding.
    embedding_model TEXT NOT NULL,

    -- Dimensionality of the embedding. Denormalized from the vector
    -- itself for quick filtering and for documentation — makes it
    -- obvious what 1024 means without inspecting the vector column.
    embedding_dim INTEGER NOT NULL DEFAULT 1024,

    -- SHA-256 hex of the content that was embedded. Lets us detect when
    -- source content has changed and the embedding is stale (content_hash
    -- here != content_hash on the source row → re-embed).
    content_hash TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Prevent double-embedding: same source row + same model = one
    -- embedding. If the content changes, the old embedding is deleted
    -- and a new one inserted (content_hash will differ).
    UNIQUE (source_table, source_id_text, embedding_model)
);

-- ivfflat index for cosine similarity search. lists = 100 is appropriate
-- for the expected scale (tens of thousands of embeddings); the rule of
-- thumb is sqrt(n) lists, and sqrt(10000) = 100.
--
-- Important: ivfflat builds centroids from existing data, so on an empty
-- table this index is created but effectively inert. After the first
-- substantial batch of inserts, run REINDEX INDEX idx_embeddings_vector
-- to rebuild the centroids from real data. At that point it's also worth
-- evaluating whether hnsw (better recall, higher write cost) would be a
-- better fit given the actual row count and query patterns.
CREATE INDEX IF NOT EXISTS idx_embeddings_vector
    ON embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
