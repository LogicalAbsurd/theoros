"""One-off: promote an existing local reflection row to Supabase.

Used for backfilling reflections that were written before the promotion
path existed, and for the initial smoke test of the promotion pipeline.

Usage:
    python -m scripts.promote_reflection --id 42 --model claude-sonnet-4-6
    python -m scripts.promote_reflection --id 42 --model claude-sonnet-4-6 --dry-run

--model is required because the local reflections table doesn't record
which Claude model produced the row (the remote schema requires
claude_model NOT NULL).  For historical rows we can only guess based on
what was configured at the time; making it a required flag forces the
operator to be honest about the guess rather than silently storing a
misleading default.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from theoros.services.promotion.supabase import (  # noqa: E402
    DAILY_PROMPT_VERSION,
    WEEKLY_PROMPT_VERSION,
    SupabaseSettings,
    get_client,
    promote_reflection,
)


DB_PATH = REPO_ROOT / "memory" / "system" / "theoros.db"


def _fetch(conn: sqlite3.Connection, row_id: int) -> dict | None:
    cur = conn.execute(
        "SELECT id, period_type, period_start, period_end, content "
        "FROM reflections WHERE id = ?",
        (row_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "period_type": row[1],
        "period_start": row[2],
        "period_end": row[3],
        "content": row[4],
    }


def _version_for(period_type: str) -> str:
    if period_type == "weekly":
        return WEEKLY_PROMPT_VERSION
    return DAILY_PROMPT_VERSION


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("theoros.promote_reflection")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, required=True, help="local reflection id")
    parser.add_argument(
        "--model", required=True,
        help="Claude model to record on the remote row (e.g. claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="fetch and print the row without inserting into Supabase",
    )
    args = parser.parse_args()

    settings = SupabaseSettings()
    if not settings.configured():
        log.error(
            "SUPABASE_URL or SUPABASE_SERVICE_KEY missing from .env — "
            "cannot promote"
        )
        return 1

    conn = sqlite3.connect(DB_PATH)
    try:
        row = _fetch(conn, args.id)
    finally:
        conn.close()

    if row is None:
        log.error("no local reflection with id=%d", args.id)
        return 1

    log.info(
        "local row: id=%d period_type=%s period_start=%s (%d chars)",
        row["id"], row["period_type"], row["period_start"], len(row["content"]),
    )

    if args.dry_run:
        log.info("dry run — not inserting into Supabase")
        return 0

    client = get_client(settings)
    remote_id = promote_reflection(
        period_type=row["period_type"],
        period_start_iso=row["period_start"],
        period_end_iso=row["period_end"],
        content=row["content"],
        claude_model=args.model,
        prompt_template_version=_version_for(row["period_type"]),
        token_usage={},  # historical row — usage was not captured at generation time
        client=client,
    )

    if remote_id is None:
        log.warning("promotion skipped (see log lines above for reason)")
        return 0

    log.info("promoted → remote id=%s", remote_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
