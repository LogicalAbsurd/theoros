"""Reader service — phone-friendly view of promoted reflections.

Read side of the Phase 5 reading surface (see docs/spec.md).  Renders
daily and weekly Claude reflections from Supabase as plain HTML behind
Cloudflare Access.  Deliberately server-rendered and JS-free — the
reader is single-operator and the surface is a personal witness log,
not an interactive app.
"""
