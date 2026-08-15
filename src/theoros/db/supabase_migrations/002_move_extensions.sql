-- =============================================================================
-- Supabase Migration 002: move extensions out of public schema
-- Date:          2026-04-23
-- Phase:         1 (Schemas)
-- Depends on:    001_initial.sql (which created the extensions in public)
--
-- This file is pasted into the Supabase SQL Editor and run once manually.
-- Every statement is idempotent so running it twice is safe.
--
-- Supabase's lint advisory flags extensions installed in the public schema
-- as a security concern — extension objects (functions, types, operators)
-- share the public namespace with application tables, which widens the
-- attack surface for privilege-escalation bugs in extension code. The
-- standard fix is to move extensions into a dedicated schema and add that
-- schema to the search_path so existing queries continue to work without
-- schema-qualifying every gen_random_uuid() or vector operator call.
--
-- Migration 001 created pgvector and pgcrypto in public (the default).
-- This migration relocates them to an `extensions` schema, grants the
-- necessary access to Supabase's default roles, and updates the
-- database-level search_path.
-- =============================================================================


-- Create the dedicated schema for extensions. IF NOT EXISTS so this is
-- safe to re-run.
CREATE SCHEMA IF NOT EXISTS extensions;

-- Grant USAGE so Supabase's default roles can call functions and use
-- types defined in the extensions schema. Without this, queries from
-- authenticated clients (PostgREST, client libraries) would fail with
-- "permission denied for schema extensions."
GRANT USAGE ON SCHEMA extensions TO postgres, anon, authenticated, service_role;

-- Move both extensions from public into the dedicated schema. ALTER
-- EXTENSION ... SET SCHEMA is idempotent in the sense that running it
-- when the extension is already in the target schema is a no-op.
ALTER EXTENSION vector SET SCHEMA extensions;
ALTER EXTENSION pgcrypto SET SCHEMA extensions;

-- Add the extensions schema to the database-wide search_path so all
-- roles resolve gen_random_uuid(), vector types, and similarity
-- operators without explicit schema qualification. This is the
-- Supabase-recommended approach — it affects the whole database, not
-- just individual roles, which is correct for a single-project
-- Supabase instance.
--
-- The "$user" entry is Postgres's default (resolves to a schema
-- matching the current role name, if one exists). Keeping it first
-- preserves standard behavior.
ALTER DATABASE postgres SET search_path TO "$user", public, extensions;
