"""Promotion pipeline — curated outputs from local SQLite to Supabase.

Per docs/spec.md: raw captures and normalized records stay local.
Only summaries, reflections, observations, themes and embeddings
are promoted to Supabase, where they power phone-readable reflection
surfaces and semantic retrieval.
"""
