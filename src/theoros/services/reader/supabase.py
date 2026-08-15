"""Read-side Supabase helpers for the reader service.

Uses the same SUPABASE_URL / SUPABASE_SERVICE_KEY as the promotion
path.  Service key bypasses RLS, which is fine — the reader is behind
Cloudflare Access, single-user, and lives on the same trusted machine
that already holds the key for writes.  The parallel RLS read policy
(migration 003) is defense-in-depth for other future readers, not the
gate for this one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from theoros.services.promotion.supabase import SupabaseSettings, get_client

log = logging.getLogger("theoros.reader.supabase")


@dataclass(frozen=True)
class ReflectionSummary:
    """List-view row: enough to render an index entry, nothing more."""

    id: str
    timeframe: str
    period_start: datetime
    period_end: datetime


@dataclass(frozen=True)
class Reflection(ReflectionSummary):
    """Full reflection with body + provenance for the detail view."""

    body_markdown: str
    claude_model: str
    prompt_template_version: str
    created_at: datetime


def _parse_ts(value: str) -> datetime:
    """Parse a PostgREST timestamptz string into a datetime.

    PostgREST returns timestamps like '2026-08-01T00:00:00+00:00' or
    with a trailing 'Z' depending on the column and driver — fromisoformat
    on 3.11+ handles both once the Z is normalised.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_reflections(limit: int = 60) -> list[ReflectionSummary]:
    """Return recent reflections, newest first, across all timeframes.

    Ordered by period_start desc so today's daily sits above last week's
    weekly.  The mixed ordering matches how the operator will actually read
    the page — chronological, most-recent-thing-first, not grouped by
    cadence.
    """
    client = get_client(SupabaseSettings())
    if client is None:
        log.warning("Supabase credentials not configured — returning empty list")
        return []

    resp = (
        client.table("reflections")
        .select("id, timeframe, period_start, period_end")
        .order("period_start", desc=True)
        .limit(limit)
        .execute()
    )
    return [_row_to_summary(row) for row in resp.data or []]


def get_reflection(timeframe: str, day: date) -> Reflection | None:
    """Look up a single reflection by (timeframe, UTC date of period_start).

    URL keys are dates rather than UUIDs so links are legible on a phone
    lock screen and stable across re-promotion.  We match by half-open
    UTC-day range on period_start rather than exact equality because the
    promoter stores period_start with millisecond precision and a
    timezone-shifted midnight (local-midnight-in-UTC), so string-equality
    lookup would need to reproduce the offset arithmetic exactly.  Range
    lookup sidesteps that.

    The promotion path enforces at-most-one row per (timeframe,
    period_start), and there is at most one canonical period per UTC day
    for each timeframe, so `.limit(1)` returns the intended row.
    """
    client = get_client(SupabaseSettings())
    if client is None:
        return None

    start_iso = f"{day.isoformat()}T00:00:00+00:00"
    end_iso = f"{(day + timedelta(days=1)).isoformat()}T00:00:00+00:00"

    resp = (
        client.table("reflections")
        .select(
            "id, timeframe, period_start, period_end, body_markdown, "
            "claude_model, prompt_template_version, created_at"
        )
        .eq("timeframe", timeframe)
        .gte("period_start", start_iso)
        .lt("period_start", end_iso)
        .limit(1)
        .execute()
    )
    if not resp.data:
        return None
    return _row_to_reflection(resp.data[0])


def _row_to_summary(row: dict[str, Any]) -> ReflectionSummary:
    return ReflectionSummary(
        id=row["id"],
        timeframe=row["timeframe"],
        period_start=_parse_ts(row["period_start"]),
        period_end=_parse_ts(row["period_end"]),
    )


def _row_to_reflection(row: dict[str, Any]) -> Reflection:
    return Reflection(
        id=row["id"],
        timeframe=row["timeframe"],
        period_start=_parse_ts(row["period_start"]),
        period_end=_parse_ts(row["period_end"]),
        body_markdown=row["body_markdown"],
        claude_model=row["claude_model"],
        prompt_template_version=row["prompt_template_version"],
        created_at=_parse_ts(row["created_at"]),
    )
