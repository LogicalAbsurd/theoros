-- =============================================================================
-- Migration 003: reflections read policy
--
-- The reflections table has row-level security enabled with no policies,
-- meaning every role except service_role is fully locked out.  The
-- promotion worker uses the service key (which bypasses RLS) and keeps
-- working; the reader service also uses the service key today, so it
-- would keep working without this migration too.
--
-- The policy is added anyway as defense-in-depth for the intended
-- posture: reflections are meant to be *read* by any authorised client,
-- with authorisation happening at the Cloudflare Access layer above the
-- reader.  Encoding that in the schema means a future client with the
-- anon key (or no key at all, once anon is opened up) can read
-- reflections without having to grant it service-role privileges.
--
-- Read only.  Writes stay locked to service_role, which continues to
-- bypass RLS.  If we ever want an operator-initiated purge from the
-- reader, that gets its own explicit DELETE policy — not implicit via
-- omitted RLS.
-- =============================================================================

-- RLS should already be enabled (the operator enabled it in the dashboard),
-- but assert it here so re-runs are idempotent and any environment
-- that skipped the manual step ends up in the same state.
ALTER TABLE reflections ENABLE ROW LEVEL SECURITY;

-- Drop-and-recreate rather than CREATE POLICY IF NOT EXISTS because
-- pre-15 Postgres lacks the IF NOT EXISTS form on policies.  Both anon
-- and authenticated roles get read access — Supabase's built-in roles
-- are the two we care about; other clients will use one of them.
DROP POLICY IF EXISTS "reflections read" ON reflections;
CREATE POLICY "reflections read"
    ON reflections
    FOR SELECT
    TO anon, authenticated
    USING (true);
