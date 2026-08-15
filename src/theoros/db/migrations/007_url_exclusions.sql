-- =============================================================================
-- Migration 007: add url_exclusions table
-- Date:          2026-05-15
-- Phase:         Mobile Capture Layer 3 (URL exclusion sync)
--
-- Establishes a single source of truth for URL exclusions, replacing
-- the extension-local store in chrome.storage.local. See
-- docs/phase-mobile-capture-layer3-design.md for the full rationale.
--
-- Two kinds: 'domain' for full-domain matches (e.g., "example.com"
-- matches any URL under that host) and 'path_prefix' for narrower
-- matches (e.g., "github.com/sensitive-org/" matches anything under
-- that path). The kinds mirror the extension's existing data model
-- so the browser extension can drop API responses straight into its
-- in-memory check without restructuring.
--
-- UNIQUE (kind, value) makes inserts idempotent, which the extension's
-- one-time migration of existing chrome.storage.local data depends on
-- for safe retry.
-- =============================================================================

BEGIN TRANSACTION;

CREATE TABLE url_exclusions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('domain', 'path_prefix')),
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (kind, value)
);

-- Index on kind for the common query pattern: "give me all domains" or
-- "give me all path_prefixes" when GET /exclusions/urls splits the
-- response into the two-list shape the extension expects.
CREATE INDEX idx_url_exclusions_kind ON url_exclusions(kind);

COMMIT;
