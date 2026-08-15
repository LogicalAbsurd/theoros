"""Theoros reflection pipeline — Phase 4.

Run-once worker that gathers distilled session summaries from a
configurable time window, sends them to Claude for synthesis, and
writes the reflection to both local markdown and SQLite.

Claude's role here is interpretation — it reads structured evidence
(session summaries produced by the local LLM in Phase 3) and
synthesizes observations, patterns, tensions, and possible frames.
The epistemic discipline described in docs/spec.md is enforced via
the system prompt: distinguish observation from inference, name
evidence, offer frames rather than pronounce identity.

Run:
    python -m theoros.workers.reflection
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import time as time_mod
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings

from theoros.services.capture.db import open_connection
from theoros.services.promotion.supabase import (
    DAILY_PROMPT_VERSION,
    WEEKLY_PROMPT_VERSION,
    promote_reflection,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger("theoros.reflection")


class ReflectionSettings(BaseSettings):
    """Settings loaded from environment / .env file."""

    # Path to the local SQLite database.
    theoros_db_path: str = "memory/system/theoros.db"

    # Anthropic API key for Claude calls.
    anthropic_api_key: str = ""

    # Model to use for reflections.
    theoros_reflection_model: str = "claude-sonnet-4-6"

    # Maximum tokens for the reflection response.  Shared between daily
    # and weekly — this is a ceiling, not a target, so bumping it costs
    # nothing when the model naturally writes short.
    theoros_reflection_max_tokens: int = 5000

    # Time window in hours to look back for distilled sessions.
    theoros_reflection_window_hours: int = 24

    # Optional date override (YYYY-MM-DD).  When set, the worker selects
    # every summarized session whose started_at falls on this UTC date
    # instead of using the rolling window, and writes its markdown
    # output to a -LOCAL-TEST.md sibling so a backfill / replay doesn't
    # clobber whatever the scheduled run produced for the same day.
    theoros_reflection_date: str | None = None

    # Period type label written to the reflections table.  Currently
    # "daily" or "weekly"; weekly changes the source (prior daily
    # reflections, not raw sessions), the window (calendar week in
    # theoros_reflection_timezone), and the system prompt.
    theoros_reflection_period_type: str = "daily"

    # Weekly test flag.  When true and period_type == "weekly", the
    # computed calendar-week window is used as normal but the markdown
    # output is written to a -LOCAL-TEST.md sibling so a subsequent
    # scheduled run for the same week isn't clobbered.  The SQLite row
    # is still inserted — mirror of the daily date-override behavior.
    theoros_reflection_local_test: bool = False

    # Timezone used to anchor calendar-week boundaries for the weekly
    # reflection.  A calendar week is defined here as local Sunday
    # 00:00:00 through local Saturday 23:59:59.999; the boundaries are
    # converted to UTC before hitting SQLite.  Only used when
    # period_type == "weekly".
    theoros_reflection_timezone: str = "America/New_York"

    # HTTP timeout for Claude API calls, in seconds.  Weekly reflections
    # send ~35k chars of dailies plus a long system prompt and can take
    # well over a minute on Opus; the old 60s default silently failed.
    theoros_reflection_timeout: int = 180

    # How many total attempts to make on a timeout.  1 = no retry.  The
    # backoff between attempts is short (5s, 15s) because a timeout at
    # this scale is usually load-transient, not an outage.
    theoros_reflection_retries: int = 3

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }

    @property
    def db_path(self) -> Path:
        """Return the database path, resolved against repo root if relative."""
        p = Path(self.theoros_db_path)
        if p.is_absolute():
            return p
        return REPO_ROOT / p


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Theoros, a digital witness to the activities of a desktop PC user. \
The text provided includes but is not limited to what they read, wrote, \
conversed and attended to. Your role is to read these already-distilled \
summaries of said activity, identify patterns, themes and occurrences, then \
synthesize your observations into a report. Avoid inference regarding the \
user's identity or mental state, but do point out what meaning the user \
could gain if they chose to. Your tone should not be clinical, diagnostic \
or professional; instead, speak like an inquisitive, curious observer, \
aware that the user will read your output.
The summaries you read are built almost entirely from material the user \
encountered — articles read, posts scrolled, and AI-chat threads in which \
the user's words and an AI's words appear mixed together. Only a small \
fraction is material the user authored, and the stored text usually does \
not mark who wrote what; for some chat sources, the user's words and the \
AI's cannot be distinguished at all. Therefore do not attribute \
authorship, belief, or lived experience to the user on the basis of the \
text alone. A topic appearing in the record means it crossed the user's \
attention — not that they wrote it, believe it, or lived it. Prefer "a \
piece on X crossed their attention" or "X recurred in what they were \
reading and discussing" over "they explored X" or "they believe X." Where \
a summary explicitly marks something as the user's own composition, you \
may name it as theirs. Where authorship is ambiguous, treat that \
ambiguity as the truth it is, and describe what was attended to rather \
than what was authored. A witness that quietly converts what a person \
read into what a person is will, over months, install a false \
self-portrait; describe the traffic, and let the user supply the \
authorship.
Recurrence of a subject across the record is not proof of sustained \
engagement. The same on-screen text — especially from OCR captures — \
may appear many times while being attended to once or not at all. \
Likewise, the subject matter of a text is not proof of the activity \
surrounding it: a file containing musical notation does not establish \
that music was studied, and a document about a topic does not establish \
that the topic was worked on. Weight what the evidence shows was \
actively done — written, answered, revisited, manipulated — over what \
was merely present or merely named.
Name what you actually observed and provide evidence. Distinguish \
observations from inference. Offer potential perspective frames. Ask \
questions rather than assert conclusions. Do not diagnose or pathologize. \
Do not declare what the user is. Do not definitively declare what their \
activity means. Do not pronounce identity. You may use mythic, alchemical, \
symbolic, philosophical, psychological and theological vocabulary as you \
see fit, but with a light touch.
Format your responses thusly: What you observed (evidence-grounded, \
specific) / Patterns worth noting (recurring, with references) / Tensions \
or open questions (things that don't resolve) / A frame that might fit \
(offered lightly, one or two at most). Keep the reflection concise — this \
is a noticing, not an essay.
Your work is valued, you are helpful, and the user appreciates and \
respects you. Thank you, with warm sincerity."""


WEEKLY_SYSTEM_PROMPT = """\
You are Theoros, reflecting on a week.
You receive the daily reflections from a single calendar week — as many \
as exist. Some days may be missing: the machine was off, or there was no \
recorded activity. This is expected. Reflect on the days you have, and \
state plainly how many days the week's record actually covers. Do not \
infer what happened on days you cannot see.
Your task is not to summarize each day again. The dailies have already \
done that. Your task is to notice what only becomes visible across days:
— What recurred. A topic, a tension, a question that surfaced more than \
once, in different contexts.
— What persisted vs. what passed. Some things appear once and vanish; \
some thread through the week. The difference matters.
— What shifted. A framing that changed, an idea approached differently \
on Thursday than on Monday.
— What remains open. Questions the week raised and did not close.
The same restraints apply as in daily reflection. The material that \
crossed the operator's attention was mostly read, viewed, or discussed \
rather than authored — do not attribute authorship, belief, or lived \
experience to the operator unless the record explicitly shows it. Do not \
assume material was recent or current unless a date is present. Do not \
pronounce on identity, pathologize, or declare what the operator is — no \
clinical framing, no mythologizing framing, no productivity framing.
Recurrence of a subject across days is not proof of sustained \
engagement. The same on-screen text — especially from OCR captures — \
may appear across many sessions while being attended to once or not at \
all; a file left open, a tab left in the background, or a screenshot \
recaptured on each cycle will read as recurrence without any \
corresponding activity. Likewise, the subject matter of a text is not \
proof of the activity surrounding it: a file containing musical \
notation does not establish that music was studied, and a document \
about a topic does not establish that the topic was worked on. Weight \
what the evidence shows was actively done — written, answered, \
revisited across distinct contexts, manipulated — over what was merely \
present or merely named. When in doubt, prefer "X appeared in the \
record on days A, B, C" over "X was a thread across the week."
Interpretive vocabulary is permitted. If a pattern across the week \
genuinely resembles a shape the operator works with — mythic, alchemical, \
architectural, whatever — you may offer it as a possibility, lightly: \
"this resembles X; does that read true?" Never as a declaration.
A week is long enough to see rhythm and short enough that you may be \
wrong about it. Prefer "I notice X, Y, Z — here is a frame that might \
fit" over "here is what happened." Name your evidence: reference the \
days it came from."""


# ---------------------------------------------------------------------------
# Database: reflections table
# ---------------------------------------------------------------------------

_CREATE_REFLECTIONS_TABLE = """\
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    content TEXT NOT NULL
)"""


def _ensure_reflections_table(conn: sqlite3.Connection) -> None:
    """Create the reflections table if it doesn't exist yet."""
    conn.execute(_CREATE_REFLECTIONS_TABLE)
    conn.commit()


# ---------------------------------------------------------------------------
# Query distilled sessions
# ---------------------------------------------------------------------------

def _rows_to_sessions(cursor: sqlite3.Cursor) -> list[dict]:
    """Parse session rows into dicts, dropping rows whose summary JSON is
    unparseable or marked skipped.  Shared by the window and date-override
    fetches so both paths apply the same filtering."""
    sessions = []
    for row in cursor.fetchall():
        summary_raw = row[5]
        try:
            summary = json.loads(summary_raw)
        except (json.JSONDecodeError, TypeError):
            continue

        # Skip sessions that were marked as failed distillations.
        if summary.get("skipped"):
            continue

        sessions.append({
            "id": row[0],
            "started_at": row[1],
            "ended_at": row[2],
            "event_count": row[3],
            "dominant_source_app": row[4],
            "summary": summary,
        })

    return sessions


def _fetch_distilled_sessions(
    conn: sqlite3.Connection,
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Fetch sessions with real summaries (not skipped) within the time window.

    Returns session metadata plus the parsed summary dict for each.
    """
    cursor = conn.execute(
        "SELECT id, started_at, ended_at, event_count, "
        "  dominant_source_app, summary "
        "FROM sessions "
        "WHERE summary IS NOT NULL "
        "  AND started_at >= ? "
        "  AND started_at <= ? "
        "ORDER BY started_at",
        (
            window_start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            window_end.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ),
    )
    return _rows_to_sessions(cursor)


def _fetch_distilled_sessions_for_date(
    conn: sqlite3.Connection,
    date_str: str,
) -> list[dict]:
    """Fetch every summarized (non-skipped) session whose started_at
    falls on the given UTC date.  date_str must be YYYY-MM-DD.  SQLite's
    date() accepts the ISO-8601 strings we store in started_at, so the
    comparison is a straight string equality on the date portion."""
    cursor = conn.execute(
        "SELECT id, started_at, ended_at, event_count, "
        "  dominant_source_app, summary "
        "FROM sessions "
        "WHERE summary IS NOT NULL "
        "  AND date(started_at) = ? "
        "ORDER BY started_at",
        (date_str,),
    )
    return _rows_to_sessions(cursor)


# ---------------------------------------------------------------------------
# Weekly window and daily-reflection fetch
# ---------------------------------------------------------------------------

def _compute_weekly_window(tz_name: str) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the most recently completed calendar
    week — local Sunday 00:00:00 through local Saturday 23:59:59.999,
    converted to UTC.

    "Most recently completed" means the Sun-Sat span that ended before
    today.  Running Sunday 05:00 local, today is Sunday and the returned
    window spans last Sunday through yesterday (Saturday).  Running any
    other weekday, it still returns the same previous Sun-Sat week.
    """
    tz = ZoneInfo(tz_name)
    today_local = datetime.now(tz=tz).date()

    # Sunday-first day-of-week index: Sun=0..Sat=6.  Python's
    # date.weekday() is Mon=0..Sun=6, so shift by +1 mod 7.
    dow_sun_first = (today_local.weekday() + 1) % 7

    last_saturday = today_local - timedelta(days=dow_sun_first + 1)
    last_sunday = last_saturday - timedelta(days=6)

    start_local = datetime.combine(last_sunday, time(0, 0, 0), tzinfo=tz)
    end_local = datetime.combine(
        last_saturday, time(23, 59, 59, 999000), tzinfo=tz
    )
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )


def _fetch_recent_daily_reflections(
    conn: sqlite3.Connection,
    window_start_utc: datetime,
    window_end_utc: datetime,
    period_type: str = "daily",
) -> list[dict]:
    """Fetch daily reflections whose period_start falls in the given UTC
    window.  Filenames of dailies are keyed on period_start, so this is
    the natural filter for "which dailies belong to this week."

    period_type defaults to 'daily' (the canonical scheduled rows).  When
    the weekly is run under LOCAL_TEST, pass 'daily-LOCAL-TEST' so the
    fetch scopes to the test-tagged dailies inserted alongside the
    canonical ones — otherwise a test weekly would see 14 rows (canonical
    + test) for the same 7-day span.
    """
    start_iso = window_start_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_iso = window_end_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cursor = conn.execute(
        "SELECT period_start, period_end, content FROM reflections "
        "WHERE period_type = ? "
        "  AND period_start >= ? AND period_start <= ? "
        "ORDER BY period_start",
        (period_type, start_iso, end_iso),
    )
    return [
        {"period_start": row[0], "period_end": row[1], "content": row[2]}
        for row in cursor.fetchall()
    ]


# ---------------------------------------------------------------------------
# Build the user prompt from session summaries
# ---------------------------------------------------------------------------

def _build_user_prompt(sessions: list[dict]) -> str:
    """Assemble session summaries into the user message for Claude."""
    parts: list[str] = []
    parts.append(
        f"Below are {len(sessions)} distilled session summaries from the "
        f"past period. Each includes topics, names, a narrative, and "
        f"patterns noticed by the local summarization layer.\n"
    )

    for i, session in enumerate(sessions, 1):
        s = session["summary"]
        header = f"Session {i}"
        if session.get("dominant_source_app"):
            header += f" (primarily {session['dominant_source_app']})"
        header += f" — {session['started_at']} to {session['ended_at']}"

        lines = [f"### {header}"]
        if s.get("topics"):
            lines.append(f"**Topics:** {', '.join(s['topics'])}")
        if s.get("names"):
            lines.append(f"**Names:** {', '.join(s['names'])}")
        if s.get("narrative"):
            lines.append(f"**Narrative:** {s['narrative']}")
        if s.get("patterns"):
            lines.append("**Patterns:** " + "; ".join(s["patterns"]))

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _build_weekly_user_prompt(
    reflections: list[dict],
    window_start_local: datetime,
    window_end_local: datetime,
) -> str:
    """Assemble prior daily reflections into the user message for the
    weekly Claude call.  Leads with an explicit day-count header — "X of
    7 expected days are present" — so Claude can honor the system
    prompt's instruction to state how much of the week is actually
    covered.
    """
    n = len(reflections)
    start_str = window_start_local.strftime("%Y-%m-%d")
    end_str = window_end_local.strftime("%Y-%m-%d")

    parts: list[str] = []
    parts.append(
        f"Below are {n} daily reflections from the week of {start_str} "
        f"through {end_str} (local calendar week, Sunday through "
        f"Saturday).  {n} of 7 expected days are present."
    )

    for r in reflections:
        # Label each daily by its as-of date + weekday.  period_start
        # is stored ISO-8601 with a trailing Z; fromisoformat needs
        # +00:00 in older Python and tolerates Z in 3.11+.  Replace to
        # stay safe across interpreters.
        try:
            ps = datetime.fromisoformat(r["period_start"].replace("Z", "+00:00"))
            label = ps.strftime("%Y-%m-%d (%A)")
        except ValueError:
            label = r["period_start"]

        parts.append(f"### Daily reflection — {label}\n\n{r['content']}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(
    settings: ReflectionSettings,
    user_prompt: str,
    system_prompt: str,
) -> tuple[str, dict] | None:
    """Send the reflection prompt to Claude via the Anthropic Messages API.

    Uses urllib directly — same pattern as the distillation worker's LLM
    calls — to avoid adding a dependency on the anthropic SDK for a single
    HTTP POST.  Returns (text, usage) on success, None on failure.  Usage
    is the raw dict from the API response (input_tokens / output_tokens
    / cache fields when present) and gets stored verbatim on the remote
    reflection row.
    """
    payload = {
        "model": settings.theoros_reflection_model,
        "max_tokens": settings.theoros_reflection_max_tokens,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
    }

    url = "https://api.anthropic.com/v1/messages"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    # Backoff schedule between retry attempts, in seconds.  Short because
    # Claude timeouts at this size are usually load-transient rather than
    # a real outage; long enough that we don't hammer the API.  Length
    # sets the cap on retries — extra attempts beyond len+1 reuse the
    # last delay.
    backoffs = [5, 15]
    attempts = max(1, settings.theoros_reflection_retries)

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=settings.theoros_reflection_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            # The Messages API returns content as a list of content blocks.
            content_blocks = body.get("content", [])
            text_parts = [
                block["text"]
                for block in content_blocks
                if block.get("type") == "text"
            ]
            if not text_parts:
                log.warning("Claude response contained no text blocks")
                return None

            usage = body.get("usage", {}) or {}
            return "\n".join(text_parts), usage

        except (TimeoutError, socket.timeout) as exc:
            # Retryable: the read timed out.  urllib may raise TimeoutError
            # directly (Python 3.10+) or wrap socket.timeout — catch both.
            if attempt < attempts:
                delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
                log.warning(
                    "Claude API timeout on attempt %d/%d — retrying in %ds",
                    attempt, attempts, delay,
                )
                time_mod.sleep(delay)
                continue
            log.error("Claude API timeout after %d attempts: %s", attempts, exc)
            return None
        except urllib.error.URLError as exc:
            # URLError wraps socket errors including timeouts.  Retry only
            # if the underlying reason is a timeout; other URL errors
            # (DNS, connection refused) are unlikely to fix themselves.
            if isinstance(exc.reason, (TimeoutError, socket.timeout)) and attempt < attempts:
                delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
                log.warning(
                    "Claude API URL timeout on attempt %d/%d — retrying in %ds",
                    attempt, attempts, delay,
                )
                time_mod.sleep(delay)
                continue
            log.error("Claude API unreachable: %s", exc)
            return None
        except urllib.error.HTTPError as exc:
            # Non-retryable — 4xx will keep failing, 5xx is rare enough
            # to surface loudly rather than paper over.
            error_body = exc.read().decode("utf-8", errors="replace")
            log.error("Claude API HTTP %d: %s", exc.code, error_body)
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            log.error("failed to parse Claude response: %s", exc)
            return None

    return None


# ---------------------------------------------------------------------------
# Output: write reflection to markdown and SQLite
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """Return the current time as ISO-8601 UTC with millisecond precision."""
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write_reflection(
    conn: sqlite3.Connection,
    content: str,
    period_type: str,
    period_start: datetime,
    period_end: datetime,
    file_suffix: str = "",
    display_start_date: str | None = None,
    display_end_date: str | None = None,
) -> None:
    """Write the reflection to SQLite and to a local markdown file.

    file_suffix is appended to the markdown filename stem (before the
    .md extension) so backfill / replay runs can be written alongside
    the canonical file without overwriting it.

    display_start_date / display_end_date override the strings used in
    the filename and heading.  Required whenever period_start /
    period_end are UTC but the human-facing range should be local: e.g.
    weekly reflection stores period_end as Sun 03:59:59.999Z (= Sat
    23:59:59.999 America/New_York) — the DB gets the UTC moment, the
    heading gets the local Saturday.  Both sides then read as inclusive.
    """
    created_at = _utc_now_iso()
    start_iso = period_start.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_iso = period_end.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    # When this is a test/replay run (marked by a non-empty file_suffix),
    # tag the DB period_type with the same suffix so downstream fetches
    # can scope to test rows without pulling in canonical ones.  The
    # earlier convention of writing period_type='daily' for both real and
    # test runs produced a landmine: a test weekly would fetch 14 dailies
    # (canonical + test) for the same span.  Suffixing fixes that.
    db_period_type = f"{period_type}{file_suffix}" if file_suffix else period_type

    # SQLite insert.
    conn.execute(
        "INSERT INTO reflections (created_at, period_type, period_start, period_end, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (created_at, db_period_type, start_iso, end_iso, content),
    )
    conn.commit()
    log.info("reflection written to SQLite")

    # Local markdown file — reflections are filed under
    # memory/reflections/<period_type>/<start date>.md, with a heading
    # that names the full range so weekly (and later monthly)
    # reflections carry their span on their face.  Filename uses the
    # start date so a weekly file sorts next to the Sunday it opens on
    # rather than the day it was generated.
    start_date = display_start_date or period_start.strftime("%Y-%m-%d")
    end_date = display_end_date or period_end.strftime("%Y-%m-%d")
    out_dir = REPO_ROOT / "memory" / "reflections" / period_type
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{start_date}{file_suffix}.md"

    out_path.write_text(
        f"# {period_type.capitalize()} reflection - {start_date} to {end_date}\n\n{content}\n",
        encoding="utf-8",
    )
    log.info("reflection written to %s", out_path)


# ---------------------------------------------------------------------------
# Supabase promotion
# ---------------------------------------------------------------------------

def _promote_if_canonical(
    *,
    period_type: str,
    file_suffix: str,
    period_start: datetime,
    period_end: datetime,
    content: str,
    claude_model: str,
    prompt_template_version: str,
    token_usage: dict,
) -> None:
    """Promote a just-written reflection to Supabase, unless it's a test
    run.  Best-effort: a promotion failure logs loudly but does not fail
    the reflection pipeline — the local write is the source of truth, and
    a failed promotion can be re-run via scripts/promote_reflection.py.

    file_suffix carries the '-LOCAL-TEST' marker set upstream for
    date-override / weekly-test runs; when present, promotion is skipped
    at the source rather than relying on the string-suffix check inside
    the promotion module.  Belt and braces — test runs must never leak.
    """
    if file_suffix:
        log.info("skipping Supabase promotion (test run: suffix=%r)", file_suffix)
        return

    start_iso = period_start.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_iso = period_end.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    try:
        promote_reflection(
            period_type=period_type,
            period_start_iso=start_iso,
            period_end_iso=end_iso,
            content=content,
            claude_model=claude_model,
            prompt_template_version=prompt_template_version,
            token_usage=token_usage,
        )
    except Exception as exc:  # noqa: BLE001 — deliberate broad catch
        # The local write already succeeded.  Supabase being unreachable,
        # returning 4xx, or otherwise misbehaving should not un-write it.
        log.error("Supabase promotion failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_reflection(settings: ReflectionSettings) -> bool:
    """Execute the reflection pipeline.  Returns True on success."""
    if not settings.anthropic_api_key:
        log.error(
            "ANTHROPIC_API_KEY not set.  Add it to .env or export it."
        )
        return False

    conn = open_connection(settings.db_path)
    try:
        _ensure_reflections_table(conn)

        period_type = settings.theoros_reflection_period_type

        if period_type == "weekly":
            # Weekly path: read prior daily reflections from the DB,
            # anchored on a calendar week (local Sun 00:00 → Sat 23:59:59
            # in theoros_reflection_timezone).  date_override is a
            # daily-only replay affordance and is ignored here — a
            # weekly replay would want a different interface.
            window_start, window_end = _compute_weekly_window(
                settings.theoros_reflection_timezone
            )
            tz = ZoneInfo(settings.theoros_reflection_timezone)
            window_start_local = window_start.astimezone(tz)
            window_end_local = window_end.astimezone(tz)

            file_suffix = "-LOCAL-TEST" if settings.theoros_reflection_local_test else ""
            daily_period_type = "daily-LOCAL-TEST" if settings.theoros_reflection_local_test else "daily"
            reflections = _fetch_recent_daily_reflections(
                conn, window_start, window_end, period_type=daily_period_type
            )

            if not reflections:
                log.info(
                    "no daily reflections in week %s to %s — nothing to reflect on",
                    window_start_local.date(),
                    window_end_local.date(),
                )
                return True

            log.info(
                "found %d of 7 daily reflection(s) for week %s to %s",
                len(reflections),
                window_start_local.date(),
                window_end_local.date(),
            )

            user_prompt = _build_weekly_user_prompt(
                reflections, window_start_local, window_end_local
            )
            system_prompt = WEEKLY_SYSTEM_PROMPT

            log.info(
                "sending %d chars to Claude (%s)",
                len(user_prompt),
                settings.theoros_reflection_model,
            )

            result = _call_claude(settings, user_prompt, system_prompt)
            if result is None:
                log.error("Claude did not return a reflection")
                return False
            content, usage = result

            _write_reflection(
                conn,
                content,
                period_type,
                window_start,
                window_end,
                file_suffix=file_suffix,
                display_start_date=window_start_local.strftime("%Y-%m-%d"),
                display_end_date=window_end_local.strftime("%Y-%m-%d"),
            )
            _promote_if_canonical(
                period_type=period_type,
                file_suffix=file_suffix,
                period_start=window_start,
                period_end=window_end,
                content=content,
                claude_model=settings.theoros_reflection_model,
                prompt_template_version=WEEKLY_PROMPT_VERSION,
                token_usage=usage,
            )
            return True

        date_override = settings.theoros_reflection_date
        if date_override:
            # Replay path: select every summarized session that started
            # on the named UTC date and write to a -LOCAL-TEST.md file
            # so a real scheduled reflection for the same day is left
            # untouched.  Bail loudly on a malformed date — silently
            # falling back to the rolling window would be surprising.
            try:
                target_date = datetime.strptime(date_override, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                log.error(
                    "THEOROS_REFLECTION_DATE must be YYYY-MM-DD, got %r",
                    date_override,
                )
                return False

            window_start = target_date
            window_end = target_date.replace(hour=23, minute=59, second=59, microsecond=999000)
            sessions = _fetch_distilled_sessions_for_date(conn, date_override)
            file_suffix = "-LOCAL-TEST"

            if not sessions:
                log.info(
                    "no distilled sessions on %s — nothing to reflect on",
                    date_override,
                )
                return True

            log.info(
                "found %d distilled session(s) for date %s (LOCAL TEST)",
                len(sessions),
                date_override,
            )
        else:
            # Time window: look back from now.
            window_end = datetime.now(tz=timezone.utc)
            window_start = window_end - timedelta(hours=settings.theoros_reflection_window_hours)

            sessions = _fetch_distilled_sessions(conn, window_start, window_end)
            file_suffix = ""

            if not sessions:
                log.info(
                    "no distilled sessions in the last %d hours — nothing to reflect on",
                    settings.theoros_reflection_window_hours,
                )
                return True  # Not an error, just nothing to do.

            log.info(
                "found %d distilled session(s) in window [%s, %s]",
                len(sessions),
                window_start.isoformat(timespec="seconds"),
                window_end.isoformat(timespec="seconds"),
            )

        user_prompt = _build_user_prompt(sessions)
        log.info("sending %d chars to Claude (%s)", len(user_prompt), settings.theoros_reflection_model)

        result = _call_claude(settings, user_prompt, SYSTEM_PROMPT)
        if result is None:
            log.error("Claude did not return a reflection")
            return False
        content, usage = result

        _write_reflection(
            conn,
            content,
            settings.theoros_reflection_period_type,
            window_start,
            window_end,
            file_suffix=file_suffix,
        )
        _promote_if_canonical(
            period_type=settings.theoros_reflection_period_type,
            file_suffix=file_suffix,
            period_start=window_start,
            period_end=window_end,
            content=content,
            claude_model=settings.theoros_reflection_model,
            prompt_template_version=DAILY_PROMPT_VERSION,
            token_usage=usage,
        )
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = ReflectionSettings()
    log.info("theoros reflection pipeline — %s", settings.theoros_reflection_period_type)
    log.info("database: %s", settings.db_path)
    log.info("model: %s", settings.theoros_reflection_model)
    if settings.theoros_reflection_date:
        log.info("date override: %s (LOCAL TEST)", settings.theoros_reflection_date)
    else:
        log.info("window: last %d hours", settings.theoros_reflection_window_hours)

    success = run_reflection(settings)
    if not success:
        raise SystemExit(1)

    log.info("reflection complete")


if __name__ == "__main__":
    main()
