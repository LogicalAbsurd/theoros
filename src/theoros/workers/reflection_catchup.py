"""Reflection catch-up worker — Phase 4.

Systemd oneshot invoked on boot.  Answers a different question from the
scheduled reflection timers: "did anything get missed while the machine
was off?" versus "is it 04:00?"  Kept as a separate unit so one
breaking never takes down the other.

Iterates the last 7 completed local calendar days (America/New_York by
default, per theoros_reflection_timezone).  For each date without a
canonical 'daily' reflection whose period_start falls on that local
date, invokes the daily pipeline for that date's UTC window and writes
canonically — not via reflection.py's THEOROS_REFLECTION_DATE
affordance, which always tags outputs -LOCAL-TEST for replay safety.

After the daily backfill, checks whether the most recently completed
calendar week (per _compute_weekly_window) has a canonical 'weekly'
reflection.  If not, delegates to run_reflection with
period_type='weekly'.  Order matters: dailies first, so a missing week
that also has missing dailies gets both filled in one pass and the
weekly sees the freshly-backfilled dailies.

systemd's own Persistent=true only fires a missed timer once, not once
per missed occurrence — so it can't backfill three days of missed
dailies on its own.  This worker does that.

Run:
    python -m theoros.workers.reflection_catchup
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from theoros.services.capture.db import open_connection
from theoros.workers.reflection import (
    ReflectionSettings,
    SYSTEM_PROMPT,
    _build_user_prompt,
    _call_claude,
    _compute_weekly_window,
    _ensure_reflections_table,
    _fetch_distilled_sessions,
    _write_reflection,
    run_reflection,
)


# How far back to look for missing dailies.  Seven days matches the operator's
# ask and also covers the longest span a weekly reflection would need.
CATCHUP_LOOKBACK_DAYS = 7

log = logging.getLogger("theoros.reflection_catchup")


# ---------------------------------------------------------------------------
# Local-day windowing
# ---------------------------------------------------------------------------

def _completed_local_dates(tz_name: str) -> list[date]:
    """Return the last CATCHUP_LOOKBACK_DAYS completed local dates —
    yesterday first, then the day before, and so on.  Today is
    excluded: it's still in progress and its reflection belongs to the
    scheduled 04:00 run, not catch-up."""
    tz = ZoneInfo(tz_name)
    today_local = datetime.now(tz=tz).date()
    return [today_local - timedelta(days=i) for i in range(1, CATCHUP_LOOKBACK_DAYS + 1)]


def _local_day_utc_window(target: date, tz_name: str) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the local calendar day `target` in
    tz_name — local 00:00:00 through local 23:59:59.999, both converted
    to UTC.  End is inclusive to match the [>=, <=] filter used by
    _fetch_distilled_sessions."""
    tz = ZoneInfo(tz_name)
    start_local = datetime.combine(target, time(0, 0, 0), tzinfo=tz)
    end_local = datetime.combine(target, time(23, 59, 59, 999000), tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Existence checks — do canonical rows already cover this period?
# ---------------------------------------------------------------------------

def _iso_ms(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _daily_exists(conn, target: date, tz_name: str) -> bool:
    """True if any canonical (period_type='daily') row has period_start
    falling within the target local day's UTC window.

    Match by presence-in-window rather than exact equality because the
    scheduled timer stores period_start as the rolling 24h window's
    start (~04:00 local previous day), while catch-up uses local
    midnight — different UTC moments, both legitimately "yesterday's
    reflection."  A window-overlap check counts either as coverage.
    """
    start_utc, end_utc = _local_day_utc_window(target, tz_name)
    cur = conn.execute(
        "SELECT 1 FROM reflections "
        "WHERE period_type = 'daily' "
        "  AND period_start >= ? AND period_start <= ? "
        "LIMIT 1",
        (_iso_ms(start_utc), _iso_ms(end_utc)),
    )
    return cur.fetchone() is not None


def _weekly_exists(conn, week_start_utc: datetime, week_end_utc: datetime) -> bool:
    """True if a canonical (period_type='weekly') row exists for the
    given UTC week window.  Weekly windows are deterministic outputs of
    _compute_weekly_window, so exact equality on period_start suffices."""
    cur = conn.execute(
        "SELECT 1 FROM reflections "
        "WHERE period_type = 'weekly' AND period_start = ? "
        "LIMIT 1",
        (_iso_ms(week_start_utc),),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Daily generation (canonical, not -LOCAL-TEST)
# ---------------------------------------------------------------------------

def _generate_daily_for_local_date(
    settings: ReflectionSettings,
    conn,
    target: date,
    tz_name: str,
) -> bool:
    """Build and write a canonical daily reflection covering the local
    day `target`.  Returns True on success or when there's nothing to
    reflect on (empty day is not an error).  False on Claude failure."""
    start_utc, end_utc = _local_day_utc_window(target, tz_name)
    sessions = _fetch_distilled_sessions(conn, start_utc, end_utc)
    if not sessions:
        log.info("no distilled sessions for %s — skipping", target)
        return True

    log.info("generating daily for %s (%d sessions)", target, len(sessions))
    user_prompt = _build_user_prompt(sessions)
    content = _call_claude(settings, user_prompt, SYSTEM_PROMPT)
    if content is None:
        log.error("Claude did not return a reflection for %s", target)
        return False

    _write_reflection(
        conn,
        content,
        "daily",
        start_utc,
        end_utc,
        file_suffix="",
        display_start_date=target.strftime("%Y-%m-%d"),
        display_end_date=target.strftime("%Y-%m-%d"),
    )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_catchup(settings: ReflectionSettings) -> bool:
    """Backfill missing dailies over the last 7 completed local days,
    then generate the most recent weekly if missing.  Returns True if
    every attempted generation succeeded (or was correctly skipped)."""
    if not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set.  Add it to .env or export it.")
        return False

    tz_name = settings.theoros_reflection_timezone
    all_ok = True

    conn = open_connection(settings.db_path)
    try:
        _ensure_reflections_table(conn)

        # Oldest to newest so the day-by-day log reads chronologically.
        for target in sorted(_completed_local_dates(tz_name)):
            if _daily_exists(conn, target, tz_name):
                log.info("daily exists for %s — skipping", target)
                continue
            if not _generate_daily_for_local_date(settings, conn, target, tz_name):
                all_ok = False
    finally:
        conn.close()

    # Weekly check runs after dailies so a missing week whose dailies
    # were also missing sees the just-backfilled dailies.
    conn = open_connection(settings.db_path)
    try:
        week_start_utc, week_end_utc = _compute_weekly_window(tz_name)
        if _weekly_exists(conn, week_start_utc, week_end_utc):
            log.info(
                "weekly exists for %s to %s (UTC) — skipping",
                week_start_utc.date(), week_end_utc.date(),
            )
        else:
            log.info(
                "weekly missing for %s to %s (UTC) — generating",
                week_start_utc.date(), week_end_utc.date(),
            )
            # Delegate to the existing weekly pipeline by cloning
            # settings with period_type flipped.  run_reflection opens
            # its own connection, which is why this block sits outside
            # the previous `with` — sqlite3 in WAL mode handles the
            # brief re-open cleanly.
            weekly_settings = settings.model_copy(
                update={"theoros_reflection_period_type": "weekly"}
            )
            if not run_reflection(weekly_settings):
                all_ok = False
    finally:
        conn.close()

    return all_ok


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = ReflectionSettings()
    log.info("theoros reflection catch-up")
    log.info("database: %s", settings.db_path)
    log.info("model: %s", settings.theoros_reflection_model)
    log.info(
        "lookback: %d completed local days in %s",
        CATCHUP_LOOKBACK_DAYS, settings.theoros_reflection_timezone,
    )

    if not run_catchup(settings):
        raise SystemExit(1)

    log.info("catch-up complete")


if __name__ == "__main__":
    main()
