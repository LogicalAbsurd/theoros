"""Supabase promotion — reflections (first surface).

Reads SUPABASE_URL and SUPABASE_SERVICE_KEY from .env (via pydantic-
settings, consistent with the rest of the codebase).  The service key
bypasses RLS, which is correct for a machine-local writer talking to
its own Supabase project — the alternative (anon key + write policies)
buys us nothing here and adds a policy-management surface.

Idempotency: before insert, we probe for an existing row with the same
(timeframe, period_start) tuple and skip if one exists.  The remote
schema has no natural unique constraint on those columns (see
docs/spec.md — reflections are conceptually append-only, but a botched
promotion shouldn't be able to duplicate a row).  A pre-insert probe
is a cheaper solution than adding a unique index and another migration.

Prompt-template versioning: the remote table requires it NOT NULL, so
we tag each promotion with a version string ("daily-v1", "weekly-v1")
that we bump when the SYSTEM_PROMPT materially changes.  This is the
hook that lets us later compare output quality across prompt iterations
without having to spelunk through git history.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic_settings import BaseSettings
from supabase import Client, create_client


REPO_ROOT = Path(__file__).resolve().parents[4]

log = logging.getLogger("theoros.promotion.supabase")


# Bump when SYSTEM_PROMPT / WEEKLY_SYSTEM_PROMPT change materially.
# The value is opaque to Supabase — it's a signal for downstream
# quality audits, not a semver contract.
DAILY_PROMPT_VERSION = "daily-v1"
WEEKLY_PROMPT_VERSION = "weekly-v1"


class SupabaseSettings(BaseSettings):
    """Supabase credentials, loaded from .env."""

    supabase_url: str = ""
    supabase_service_key: str = ""

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }

    def configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)


def get_client(settings: SupabaseSettings | None = None) -> Client | None:
    """Return a service-role Supabase client, or None if creds are absent.

    Absent-creds is not raised as an error — the promotion path treats
    Supabase as best-effort so a local machine without creds still runs
    the local reflection pipeline cleanly.  Callers decide whether to
    log-and-skip or fail hard.
    """
    settings = settings or SupabaseSettings()
    if not settings.configured():
        return None
    return create_client(settings.supabase_url, settings.supabase_service_key)


def _local_period_type_to_timeframe(period_type: str) -> str | None:
    """Map a local period_type to a remote timeframe, or None to skip.

    Local rows tagged with a -LOCAL-TEST suffix are test artifacts and
    would fail the remote CHECK constraint (timeframe IN daily/weekly/
    monthly); we skip them here rather than let PostgREST reject them.
    """
    if period_type.endswith("-LOCAL-TEST"):
        return None
    if period_type in ("daily", "weekly", "monthly"):
        return period_type
    return None


def _row_already_promoted(
    client: Client, timeframe: str, period_start_iso: str
) -> bool:
    """Return True if a reflection with this (timeframe, period_start)
    already exists in Supabase.  Cheap idempotency guard — see module
    docstring for why we don't rely on a unique constraint."""
    resp = (
        client.table("reflections")
        .select("id")
        .eq("timeframe", timeframe)
        .eq("period_start", period_start_iso)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


def promote_reflection(
    *,
    period_type: str,
    period_start_iso: str,
    period_end_iso: str,
    content: str,
    claude_model: str,
    prompt_template_version: str,
    token_usage: dict | None = None,
    evidence_refs: list | None = None,
    client: Client | None = None,
) -> str | None:
    """Insert a reflection row into Supabase.

    Returns the remote row's UUID on success, None if skipped
    (unconfigured creds, LOCAL-TEST row, or already-promoted).  Raises
    on real Supabase errors — silent failure would let promotion drift
    from local reality without anyone noticing.
    """
    timeframe = _local_period_type_to_timeframe(period_type)
    if timeframe is None:
        log.info("skipping promotion: period_type=%r not promotable", period_type)
        return None

    if client is None:
        client = get_client()
        if client is None:
            log.info("skipping promotion: Supabase credentials not configured")
            return None

    if _row_already_promoted(client, timeframe, period_start_iso):
        log.info(
            "skipping promotion: reflection for (%s, %s) already in Supabase",
            timeframe, period_start_iso,
        )
        return None

    row = {
        "timeframe": timeframe,
        "period_start": period_start_iso,
        "period_end": period_end_iso,
        "body_markdown": content,
        "evidence_refs": evidence_refs or [],
        "claude_model": claude_model,
        "prompt_template_version": prompt_template_version,
        "token_usage": token_usage or {},
    }

    resp = client.table("reflections").insert(row).execute()
    if not resp.data:
        raise RuntimeError(f"Supabase insert returned no data: {resp}")

    remote_id = resp.data[0]["id"]
    log.info(
        "promoted %s reflection for period_start=%s → %s",
        timeframe, period_start_iso, remote_id,
    )
    return remote_id
