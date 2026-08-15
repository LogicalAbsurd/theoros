"""Capture heartbeat: is each tier still producing rows, and are the
services that should be feeding it actually running?

One screen of output.  No alerting, no history — a snapshot answer to
"is the pipeline alive right now?".  Alerting is a later design
conversation; this command exists so the operator can eyeball the pipeline
without hand-writing SQL every time.

Tiers checked: browser, atspi, ocr, window_meta, mobile.  Desktop tiers
default to a 2-hour silence threshold, mobile to 6h (phone push cadence
is naturally sparser and hits gaps during charging/sleep).  Overridable
per-invocation with --desktop-threshold / --mobile-threshold.

Services checked: only the systemd user units that actually exist for
capture on this machine — theoros-capture (browser bridge),
theoros-ocr, theoros-window-meta.  atspi has no service yet; mobile is
push-from-phone with no local process to check.  Those two tiers show
"(no local service)" in the services table.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]
_DB_PATH = _REPO_ROOT / "memory" / "system" / "theoros.db"

# The full tier set the heartbeat reports on.  Kept as a constant so a
# tier that has never produced a row still appears in output — a silent
# absence is exactly what we want to see.
_DESKTOP_TIERS = ("browser", "atspi", "ocr", "window_meta")
_MOBILE_TIERS = ("mobile",)
_ALL_TIERS = _DESKTOP_TIERS + _MOBILE_TIERS

# Map each tier to the systemd user unit responsible for feeding it.
# None means "no local service exists for this tier" — reported as
# such rather than shown as failing.
_TIER_SERVICE: dict[str, str | None] = {
    "browser": "theoros-capture.service",
    "atspi": None,
    "ocr": "theoros-ocr.service",
    "window_meta": "theoros-window-meta.service",
    "mobile": None,
}

# Optional annotation shown next to a tier so "expected absence" reads
# distinctly from "broken."  A NEVER on atspi is not a failure — the
# daemon isn't built yet.  Without this note, future-you will spend
# ten minutes rediscovering that.
_TIER_NOTE: dict[str, str] = {
    "atspi": "(no daemon deployed)",
}

# ANSI colors.  Kept minimal — green/yellow/red for status, dim for
# metadata, reset.  Disabled if stdout is not a TTY so piping to a file
# doesn't get littered with escape codes.
_ANSI = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _color(name: str, text: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{_ANSI[name]}{text}{_ANSI['reset']}"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "health",
        help="Capture heartbeat: last event per tier, service status, last reflection.",
    )
    parser.add_argument(
        "--desktop-threshold", type=float, default=2.0,
        help="Hours of silence before a desktop tier is flagged (default: 2).",
    )
    parser.add_argument(
        "--mobile-threshold", type=float, default=6.0,
        help="Hours of silence before a mobile tier is flagged (default: 6).",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color output.",
    )
    parser.set_defaults(func=run)


def _open_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp from raw_events.captured_at.

    The column stores either "...Z" or "...+00:00" depending on which
    capture path wrote it; fromisoformat handles the latter natively and
    we normalize the former by hand.  Returns None on unparseable input
    rather than raising — the heartbeat should degrade gracefully if a
    single row has a malformed timestamp.
    """
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return None


def _format_age(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "future?"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d{hours:02d}h"
    return f"{hours:02d}h{minutes:02d}m"


def _service_status(unit: str) -> tuple[str, bool]:
    """Return (state, is_active) for a systemd user unit.

    Uses `systemctl --user is-active` because it's the cheapest call
    that gives a one-word answer and doesn't require parsing.  Returns
    ("unknown", False) if systemctl isn't on PATH — we don't want the
    heartbeat to crash on a system where systemd isn't the init.
    """
    if shutil.which("systemctl") is None:
        return ("no-systemctl", False)
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        return ("timeout", False)
    state = (result.stdout or result.stderr).strip() or "unknown"
    return (state, state == "active")


def _fetch_last_captured(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Return {tier: last_captured_at_iso_or_None} for every tier in _ALL_TIERS.

    Left-joins in Python rather than in SQL: the tier set is fixed and
    tiny, so we ask for MAX() over tiers that have any rows and fill
    missing tiers with None.  This surfaces "tier has zero rows ever"
    as a distinct state from "tier has rows but is stale."
    """
    rows = conn.execute(
        "SELECT source_tier, MAX(captured_at) FROM raw_events GROUP BY source_tier"
    ).fetchall()
    by_tier = {tier: ts for tier, ts in rows}
    return {tier: by_tier.get(tier) for tier in _ALL_TIERS}


def _fetch_last_reflection_by_period(
    conn: sqlite3.Connection, period_type: str,
) -> tuple[str, str] | None:
    """Return (created_at, period_end) for the most recent reflection of
    a given period_type, or None if none exist.

    Scoped per period_type so a fresh weekly doesn't mask a stale daily
    — that failure mode is exactly what this health check exists to
    surface.
    """
    return conn.execute(
        "SELECT created_at, period_end FROM reflections "
        "WHERE period_type = ? ORDER BY created_at DESC LIMIT 1",
        (period_type,),
    ).fetchone()


def run(args: argparse.Namespace) -> None:
    if not _DB_PATH.exists():
        print(f"Error: database not found at {_DB_PATH}", file=sys.stderr)
        sys.exit(1)

    use_color = (not args.no_color) and sys.stdout.isatty()
    now = datetime.now(timezone.utc)
    desktop_thresh = timedelta(hours=args.desktop_threshold)
    mobile_thresh = timedelta(hours=args.mobile_threshold)

    conn = _open_connection(_DB_PATH)
    try:
        last_by_tier = _fetch_last_captured(conn)
        last_daily = _fetch_last_reflection_by_period(conn, "daily")
        last_weekly = _fetch_last_reflection_by_period(conn, "weekly")
    finally:
        conn.close()

    print(_color("bold", f"theoros heartbeat  {now.isoformat(timespec='seconds')}", use_color))
    print()

    # ---- Capture tiers -------------------------------------------------
    print(_color("bold", "capture tiers", use_color))
    for tier in _ALL_TIERS:
        threshold = mobile_thresh if tier in _MOBILE_TIERS else desktop_thresh
        last_iso = last_by_tier.get(tier)
        last_dt = _parse_iso(last_iso) if last_iso else None

        if last_dt is None:
            status = _color("red", "NEVER", use_color)
            age_str = "—"
            last_display = "—"
        else:
            age = now - last_dt
            silent = age > threshold
            status = (
                _color("red", "STALE", use_color) if silent
                else _color("green", "OK   ", use_color)
            )
            age_str = _format_age(age)
            last_display = last_dt.isoformat(timespec="seconds")

        thresh_hours = threshold.total_seconds() / 3600
        thresh_note = _color("dim", f"(>{thresh_hours:g}h)", use_color)
        annotation = _TIER_NOTE.get(tier, "")
        annotation_str = f"  {_color('dim', annotation, use_color)}" if annotation else ""
        print(
            f"  {status}  {tier:<12} last={last_display}  age={age_str}  "
            f"{thresh_note}{annotation_str}"
        )
    print()

    # ---- Services ------------------------------------------------------
    print(_color("bold", "capture services", use_color))
    for tier in _ALL_TIERS:
        unit = _TIER_SERVICE[tier]
        if unit is None:
            note = _color("dim", "(no local service)", use_color)
            print(f"  {_color('dim', '----', use_color)}  {tier:<12} {note}")
            continue
        state, is_active = _service_status(unit)
        status = (
            _color("green", "UP  ", use_color) if is_active
            else _color("red", "DOWN", use_color)
        )
        print(f"  {status}  {tier:<12} {unit}  [{state}]")
    print()

    # ---- Reflections ---------------------------------------------------
    # Reported per period_type: a fresh weekly must not mask a stale
    # daily, since the two cadences fail independently.
    print(_color("bold", "last reflection", use_color))
    for period_type, row in (("daily", last_daily), ("weekly", last_weekly)):
        if row is None:
            print(f"  {period_type:<8} {_color('yellow', 'NONE', use_color)}  none written yet")
            continue
        created_at, period_end = row
        created_dt = _parse_iso(created_at)
        age_str = _format_age(now - created_dt) if created_dt else "?"
        print(
            f"  {period_type:<8} covering through {period_end}  "
            f"written {created_at}  ({age_str} ago)"
        )
    print()
