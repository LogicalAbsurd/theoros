"""Purge raw_events rows within a time range.

Deletes all rows from raw_events whose captured_at falls within
[--since, --until). Checks for partially-affected chains and refuses
to proceed unless --force-partial-chain is set.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Database path — same resolution as theoros.db.migrate
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DB_PATH = _REPO_ROOT / "memory" / "system" / "theoros.db"


def _open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def _parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 datetime string with timezone info.

    Accepts formats like '2026-04-29T20:00:00Z' and
    '2026-04-29T20:00:00+00:00'.
    """
    # Python 3.11+ fromisoformat handles 'Z' suffix.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp must include timezone: {value}")
    return dt


def _format_utc(dt: datetime) -> str:
    """Format a datetime as ISO-8601 UTC with millisecond precision."""
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Subcommand registration
# ---------------------------------------------------------------------------

def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the purge subcommand."""
    parser = subparsers.add_parser(
        "purge",
        help="Delete raw_events rows within a time range.",
    )
    parser.add_argument(
        "--since", required=True,
        help="ISO-8601 UTC timestamp; rows with captured_at >= this are candidates.",
    )
    parser.add_argument(
        "--until", required=True,
        help="ISO-8601 UTC timestamp; rows with captured_at < this are candidates.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be deleted without deleting.",
    )
    parser.add_argument(
        "--force-partial-chain", action="store_true",
        help="Allow deletion that leaves chains partially intact.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    parser.set_defaults(func=run)


# ---------------------------------------------------------------------------
# Command implementation
# ---------------------------------------------------------------------------

_SUPABASE_NOTE = (
    "Note: Supabase tables (session_summaries, reflections, observations, "
    "embeddings) are not yet populated by Theoros. When they are, purge will "
    "need to also delete corresponding rows. For now, only local SQLite was "
    "affected."
)


def run(args: argparse.Namespace) -> None:
    """Execute the purge command."""
    # --- Parse timestamps ---
    try:
        since = _parse_iso_datetime(args.since)
    except ValueError as exc:
        print(f"Error: invalid --since: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        until = _parse_iso_datetime(args.until)
    except ValueError as exc:
        print(f"Error: invalid --until: {exc}", file=sys.stderr)
        sys.exit(1)

    if until <= since:
        print("Error: --until must be strictly greater than --since.", file=sys.stderr)
        sys.exit(1)

    since_str = _format_utc(since)
    until_str = _format_utc(until)

    # --- Open database ---
    if not _DB_PATH.exists():
        print(f"Error: database not found at {_DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = _open_connection(_DB_PATH)
    try:
        _run_purge(conn, since_str, until_str, args)
    finally:
        conn.close()


def _run_purge(
    conn: sqlite3.Connection,
    since_str: str,
    until_str: str,
    args: argparse.Namespace,
) -> None:
    """Core purge logic, separated for testability."""
    # --- Find candidate rows ---
    candidates = conn.execute(
        "SELECT id, captured_at, chain_id, chain_position FROM raw_events "
        "WHERE captured_at >= ? AND captured_at < ?",
        (since_str, until_str),
    ).fetchall()

    if not candidates:
        print(f"No rows found in range [{since_str}, {until_str}).")
        print()
        print(_SUPABASE_NOTE)
        return

    candidate_count = len(candidates)

    # --- Check for partially-affected chains ---
    partial_chains = conn.execute(
        "SELECT chain_id, COUNT(*) AS total, "
        "  SUM(CASE WHEN captured_at >= ? AND captured_at < ? THEN 1 ELSE 0 END) AS in_range "
        "FROM raw_events "
        "WHERE chain_id IS NOT NULL "
        "GROUP BY chain_id "
        "HAVING in_range > 0 AND in_range < total",
        (since_str, until_str),
    ).fetchall()

    if partial_chains:
        if not args.force_partial_chain:
            print(
                f"Refusing to delete: {len(partial_chains)} chain(s) would be "
                f"partially deleted. Use --force-partial-chain to override.",
                file=sys.stderr,
            )
            print(file=sys.stderr)
            print(f"  {'chain_id':<38} {'total':>5}  {'in_range':>8}", file=sys.stderr)
            print(f"  {'-' * 38} {'-' * 5}  {'-' * 8}", file=sys.stderr)
            for chain_id, total, in_range in partial_chains:
                print(f"  {chain_id:<38} {total:>5}  {in_range:>8}", file=sys.stderr)
            sys.exit(1)
        else:
            print(
                f"Warning: {len(partial_chains)} chain(s) will be partially deleted."
            )
            print(f"  {'chain_id':<38} {'total':>5}  {'in_range':>8}")
            print(f"  {'-' * 38} {'-' * 5}  {'-' * 8}")
            for chain_id, total, in_range in partial_chains:
                print(f"  {chain_id:<38} {total:>5}  {in_range:>8}")
            print()

    # --- Summarize ---
    # Distinct chains and per-chain counts from the candidate set.
    chain_counts: dict[str, int] = {}
    for _, _, chain_id, _ in candidates:
        if chain_id:
            chain_counts[chain_id] = chain_counts.get(chain_id, 0) + 1

    print(f"Time range: [{since_str}, {until_str})")
    print(f"Rows to delete: {candidate_count}")
    print(f"Chains affected: {len(chain_counts)}")
    if chain_counts:
        for cid, count in chain_counts.items():
            print(f"  {cid[:8]}… — {count} row(s)")
    print()
    print(_SUPABASE_NOTE)
    print()

    # --- Dry run exits here ---
    if args.dry_run:
        print("Dry run — no changes made.")
        return

    # --- Confirmation ---
    if not args.yes:
        try:
            answer = input(f"Delete {candidate_count} rows? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print()
            print("Cancelled.")
            return
        if answer.strip().lower() != "y":
            print("Cancelled.")
            return

    # --- Delete ---
    cursor = conn.execute(
        "DELETE FROM raw_events WHERE captured_at >= ? AND captured_at < ?",
        (since_str, until_str),
    )
    conn.commit()
    deleted = cursor.rowcount

    print(f"Deleted {deleted} rows from raw_events.")
