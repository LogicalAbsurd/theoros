"""Ad-hoc re-distillation for a targeted set of sessions.

Unlike the main distillation worker, this script does not scan for
`summary IS NULL` rows.  It processes only the session IDs you pass on
the command line — either as explicit IDs or as a date range (UTC).
Existing summaries for those sessions are overwritten.

Written for the 2026-07-25 prompt-rename test: we want to re-distill
2026-07-23 and 2026-07-24 with the new SYSTEM_PROMPT without touching
the other ~295 undistilled sessions sitting in the queue.

Usage:
    # by date (UTC, based on sessions.started_at)
    python scripts/redistill_sessions.py --date 2026-07-23 --date 2026-07-24

    # by explicit IDs
    python scripts/redistill_sessions.py --id 1230 --id 1232 --id 1233

    # lower the min-events floor so short sessions (e.g. 1230, 2 events)
    # are included; default matches the worker's default of 3
    python scripts/redistill_sessions.py --date 2026-07-23 --min-events 2

    # dry run — show what would be re-distilled and stop
    python scripts/redistill_sessions.py --date 2026-07-23 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys

from theoros.services.capture.db import open_connection
from theoros.workers.distillation import (
    DistillationSettings,
    _call_local_llm,
    _gather_session_text,
    _make_skip_summary,
    _write_summary,
)

log = logging.getLogger("theoros.redistill")


def _resolve_session_ids(
    conn,
    dates: list[str],
    explicit_ids: list[int],
    min_events: int,
) -> list[dict]:
    """Return session rows matching the CLI filters, ordered by start time."""
    where_parts: list[str] = []
    params: list = []

    if dates:
        placeholders = ",".join("?" for _ in dates)
        where_parts.append(f"date(started_at) IN ({placeholders})")
        params.extend(dates)

    if explicit_ids:
        placeholders = ",".join("?" for _ in explicit_ids)
        where_parts.append(f"id IN ({placeholders})")
        params.extend(explicit_ids)

    if not where_parts:
        raise SystemExit("must pass at least one --date or --id")

    # OR the filters together — a session matches if it's in the date
    # range OR explicitly listed.  Union semantics is friendlier than
    # intersection for ad-hoc re-runs.
    where = " OR ".join(where_parts)
    cursor = conn.execute(
        f"SELECT id, started_at, ended_at, event_count, dominant_source_app "
        f"FROM sessions "
        f"WHERE ({where}) AND event_count >= ? "
        f"ORDER BY started_at",
        (*params, min_events),
    )
    return [
        {
            "id": row[0],
            "started_at": row[1],
            "ended_at": row[2],
            "event_count": row[3],
            "dominant_source_app": row[4],
        }
        for row in cursor.fetchall()
    ]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", action="append", default=[],
                        help="UTC date YYYY-MM-DD; may be repeated")
    parser.add_argument("--id", type=int, action="append", default=[],
                        dest="ids", help="explicit session id; may be repeated")
    parser.add_argument("--min-events", type=int, default=None,
                        help="override min event count (default: worker's setting)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list target sessions and exit without distilling")
    args = parser.parse_args()

    settings = DistillationSettings()
    min_events = (
        args.min_events
        if args.min_events is not None
        else settings.theoros_distillation_min_events
    )

    conn = open_connection(settings.db_path)
    try:
        targets = _resolve_session_ids(conn, args.date, args.ids, min_events)
        if not targets:
            log.warning("no sessions matched filters")
            return

        log.info("targeting %d session(s):", len(targets))
        for s in targets:
            log.info("  id=%d  %s → %s  (%d events)",
                     s["id"], s["started_at"], s["ended_at"], s["event_count"])

        if args.dry_run:
            log.info("dry-run: no changes made")
            return

        distilled = 0
        skipped = 0
        for session in targets:
            sid = session["id"]
            log.info("distilling session %d", sid)
            text = _gather_session_text(
                conn, sid, settings.theoros_distillation_max_text_chars
            )
            if not text.strip():
                log.warning("session %d has no text content, skipping", sid)
                continue

            # Retry inline rather than across cycles — this is a one-shot
            # script, not a long-running worker.
            summary = None
            attempts = 0
            for attempts in range(1, settings.theoros_distillation_max_retries + 1):
                summary = _call_local_llm(settings, text, session)
                if summary is not None:
                    break
                log.warning("session %d attempt %d/%d failed",
                            sid, attempts,
                            settings.theoros_distillation_max_retries)

            if summary is None:
                log.warning("session %d exhausted retries, writing skip summary", sid)
                _write_summary(conn, sid, _make_skip_summary(session, attempts))
                skipped += 1
                continue

            _write_summary(conn, sid, summary)
            distilled += 1
            log.info("session %d re-distilled: %d topics, %d names",
                     sid,
                     len(summary.get("topics", [])),
                     len(summary.get("names", [])))

        log.info("done — %d re-distilled, %d skipped", distilled, skipped)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
