"""Theoros session detector — Phase 3.

Periodic worker that finds raw_events with no session_id, groups them
into sessions using idle-threshold heuristics, creates session rows,
and backfills the session_id foreign key on each member event.

The primary splitting rule is temporal: a gap longer than the idle
threshold between consecutive events starts a new session.  Within
each session, the dominant source_app is computed — if one app accounts
for more than the dominance threshold of events, it's recorded as the
session's dominant_source_app; otherwise the field is NULL.

The trailing group of events (the most recent activity) is held open
if the last event is younger than the idle threshold — it might still
be accumulating events from the capture layer.

Run:
    python -m theoros.workers.session_detector
"""

from __future__ import annotations

import logging
import signal
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from pydantic_settings import BaseSettings

from theoros.services.capture.db import open_connection


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# session_detector.py lives at src/theoros/workers/ — three parents up
# to the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger("theoros.session_detector")

# Recorded in sessions.detection_algorithm_version so future algorithm
# revisions can coexist with v1 sessions without ambiguity.
ALGORITHM_VERSION = "v1"


class SessionDetectorSettings(BaseSettings):
    """Settings loaded from environment / .env file."""

    # Path to the local SQLite database.  If relative, resolved against
    # the repo root.
    theoros_db_path: str = "memory/system/theoros.db"

    # Seconds between detection passes.
    theoros_session_detector_interval: int = 300  # 5 minutes

    # A gap longer than this (in seconds) between consecutive events
    # starts a new session.  Five minutes matches reasonable desktop
    # idle behaviour — stepping away for coffee, reading a long page
    # without interaction, switching to a phone call.
    theoros_session_idle_threshold: int = 300  # 5 minutes

    # Fraction of events from a single source_app needed to count as
    # dominant.  Below this, dominant_source_app is NULL — the session
    # was mixed.
    theoros_session_dominance_threshold: float = 0.70

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        # The .env file contains THEOROS_* vars for other services
        # (LLM URL, capture host, etc.) — ignore them here.
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
# Helpers
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """Return the current time as ISO-8601 UTC with millisecond precision."""
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp as stored in the database.

    Handles both '...Z' and '...+00:00' suffixes.  Python's
    datetime.fromisoformat() gained 'Z' support in 3.11, but we
    normalize explicitly so the code is obvious.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def _check_tables(db_path: Path) -> None:
    """Verify that sessions and raw_events tables exist."""
    conn = open_connection(db_path)
    try:
        for table in ("sessions", "raw_events"):
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name=?",
                (table,),
            )
            if cursor.fetchone() is None:
                raise RuntimeError(
                    f"{table} table not found in {db_path}.  "
                    f"Run `python -m theoros.db.migrate` to create the schema."
                )
    finally:
        conn.close()


def _fetch_unassigned_events(
    conn: sqlite3.Connection,
) -> list[tuple[int, str, str | None]]:
    """Fetch raw_events that have no session_id, ordered chronologically.

    Returns (id, captured_at, source_app) tuples — the minimum needed
    for grouping.  We don't load content here; that's the distillation
    worker's job.
    """
    cursor = conn.execute(
        "SELECT id, captured_at, source_app "
        "FROM raw_events "
        "WHERE session_id IS NULL "
        "ORDER BY captured_at"
    )
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# Grouping logic
# ---------------------------------------------------------------------------

def _group_into_sessions(
    events: list[tuple[int, str, str | None]],
    idle_threshold: int,
) -> list[list[tuple[int, str, str | None]]]:
    """Split chronologically-ordered events into session groups.

    A new session starts whenever the gap between consecutive events
    exceeds idle_threshold seconds.

    The final group is excluded if its most recent event is younger
    than idle_threshold — it's still accumulating and will be picked
    up in a later cycle.
    """
    if not events:
        return []

    groups: list[list[tuple[int, str, str | None]]] = []
    current: list[tuple[int, str, str | None]] = [events[0]]

    for event in events[1:]:
        prev_time = _parse_iso(current[-1][1])
        curr_time = _parse_iso(event[1])
        gap = (curr_time - prev_time).total_seconds()

        if gap > idle_threshold:
            groups.append(current)
            current = [event]
        else:
            current.append(event)

    # Hold the trailing group open if its last event is recent — more
    # events may still arrive from the capture layer.
    if current:
        last_time = _parse_iso(current[-1][1])
        age = (datetime.now(tz=timezone.utc) - last_time).total_seconds()
        if age > idle_threshold:
            groups.append(current)
        else:
            log.debug(
                "holding %d event(s) in open session (last event %ds ago)",
                len(current),
                int(age),
            )

    return groups


def _dominant_app(
    events: list[tuple[int, str, str | None]],
    threshold: float,
) -> str | None:
    """Return the dominant source_app if it exceeds the threshold.

    Events with source_app = NULL are excluded from the count — they
    don't vote for or against any app.  If every event has NULL
    source_app, returns None.
    """
    apps = [e[2] for e in events if e[2] is not None]
    if not apps:
        return None

    counts = Counter(apps)
    top_app, top_count = counts.most_common(1)[0]

    if top_count / len(apps) >= threshold:
        return top_app
    return None


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------

def _create_session(
    conn: sqlite3.Connection,
    events: list[tuple[int, str, str | None]],
    dominant_source_app: str | None,
) -> int:
    """Insert a session row and backfill session_id on member events.

    Runs inside a single transaction so the session row and the FK
    updates are atomic — no dangling session_ids if the process is
    killed mid-write.
    """
    started_at = events[0][1]
    ended_at = events[-1][1]
    event_count = len(events)
    created_at = _utc_now_iso()

    cursor = conn.execute(
        "INSERT INTO sessions "
        "(started_at, ended_at, event_count, dominant_source_app, "
        " detection_algorithm_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            started_at,
            ended_at,
            event_count,
            dominant_source_app,
            ALGORITHM_VERSION,
            created_at,
        ),
    )
    session_id = cursor.lastrowid

    # Backfill in batches — SQLite's variable limit is 999 in older
    # builds; 500 per batch stays well within that.
    event_ids = [e[0] for e in events]
    for i in range(0, len(event_ids), 500):
        batch = event_ids[i : i + 500]
        placeholders = ",".join("?" * len(batch))
        conn.execute(
            f"UPDATE raw_events SET session_id = ? "
            f"WHERE id IN ({placeholders})",
            [session_id, *batch],
        )

    conn.commit()
    return session_id


# ---------------------------------------------------------------------------
# Detection cycle
# ---------------------------------------------------------------------------

def _detection_cycle(settings: SessionDetectorSettings) -> int:
    """Run one detection pass.  Returns the number of sessions created."""
    conn = open_connection(settings.db_path)
    try:
        events = _fetch_unassigned_events(conn)
        if not events:
            log.debug("no unassigned events found")
            return 0

        log.info("found %d unassigned event(s)", len(events))

        groups = _group_into_sessions(
            events, settings.theoros_session_idle_threshold
        )
        if not groups:
            log.debug("no complete sessions to create (trailing group held open)")
            return 0

        created = 0
        for group in groups:
            app = _dominant_app(group, settings.theoros_session_dominance_threshold)
            sid = _create_session(conn, group, app)
            created += 1
            log.info(
                "session %d: %s to %s, %d events, dominant_app=%s",
                sid,
                group[0][1],
                group[-1][1],
                len(group),
                app or "(none)",
            )

        return created
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

    settings = SessionDetectorSettings()
    log.info("starting theoros session detector")
    log.info("database: %s", settings.db_path)
    log.info(
        "interval: %ds, idle threshold: %ds, dominance threshold: %.0f%%",
        settings.theoros_session_detector_interval,
        settings.theoros_session_idle_threshold,
        settings.theoros_session_dominance_threshold * 100,
    )

    _check_tables(settings.db_path)

    shutdown = Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("received signal %d, shutting down", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Initial cycle immediately — don't wait a full interval before
    # processing events that are already queued.
    try:
        created = _detection_cycle(settings)
        if created:
            log.info("initial pass: created %d session(s)", created)
    except Exception:
        log.exception("error in initial detection cycle")

    while not shutdown.is_set():
        shutdown.wait(timeout=settings.theoros_session_detector_interval)
        if shutdown.is_set():
            break
        try:
            _detection_cycle(settings)
        except Exception:
            log.exception("error in detection cycle")

    log.info("theoros session detector stopped")


if __name__ == "__main__":
    main()
