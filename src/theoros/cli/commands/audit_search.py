"""Search raw_events for a term across content_text, delta_text, and title.

Audit helper for the question "did this term in a summary/reflection trace
back to actual captured evidence?".  A plain LIKE on content_text alone
misses anything captured incrementally — chain-follower rows carry their
new text in delta_text, and the distiller only sees the term because
reconstruct_chain_text reassembles the full chain.  This command searches
all three columns so an auditor sees what the distiller saw.

Not full-text search.  Substring-match only, case-insensitive by default.
If audits get heavy enough to need ranking or phrase queries, swap to an
FTS5 virtual table over reconstructed chain text — a bigger build than
this admin tool warrants right now.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]
_DB_PATH = _REPO_ROOT / "memory" / "system" / "theoros.db"


def _open_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "audit-search",
        help="Find a term in raw_events across content_text, delta_text, and title.",
    )
    parser.add_argument(
        "term",
        help="Substring to search for.  Case-insensitive unless --case-sensitive.",
    )
    parser.add_argument(
        "--since",
        help="ISO-8601 UTC timestamp; only match rows with captured_at >= this.",
    )
    parser.add_argument(
        "--until",
        help="ISO-8601 UTC timestamp; only match rows with captured_at < this.",
    )
    parser.add_argument(
        "--session", type=int,
        help="Limit to a single session_id.",
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Maximum rows to print (default: 50).",
    )
    parser.add_argument(
        "--context", type=int, default=200,
        help="Characters of context shown around the match (default: 200).",
    )
    parser.add_argument(
        "--case-sensitive", action="store_true",
        help="Match exact case instead of case-insensitive.",
    )
    parser.set_defaults(func=run)


def _snippet(haystack: str, needle_lower: str, context: int) -> str:
    """Return a context-window snippet around the first match.

    Searches case-insensitively to locate the match, then slices from
    the original string so case is preserved in output.  Collapses
    interior whitespace so long delta blobs print as a single readable
    line.
    """
    idx = haystack.lower().find(needle_lower)
    if idx < 0:
        return ""
    start = max(0, idx - context)
    end = min(len(haystack), idx + len(needle_lower) + context)
    slice_ = haystack[start:end]
    cleaned = " ".join(slice_.split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(haystack) else ""
    return f"{prefix}{cleaned}{suffix}"


def run(args: argparse.Namespace) -> None:
    if not _DB_PATH.exists():
        print(f"Error: database not found at {_DB_PATH}", file=sys.stderr)
        sys.exit(1)

    # Build the WHERE clause.  We always search all three columns; the
    # case-sensitive flag picks between LIKE and a case-insensitive
    # variant.  SQLite's default LIKE is case-insensitive for ASCII only,
    # so for non-ASCII terms we lower() both sides explicitly.
    term = args.term
    if args.case_sensitive:
        col_predicate = (
            "(content_text GLOB ? OR delta_text GLOB ? OR title GLOB ?)"
        )
        # GLOB is case-sensitive in SQLite; wrap with * for substring.
        pattern: str | tuple = (f"*{term}*", f"*{term}*", f"*{term}*")
    else:
        col_predicate = (
            "(lower(content_text) LIKE ? "
            " OR lower(delta_text) LIKE ? "
            " OR lower(title) LIKE ?)"
        )
        like_term = f"%{term.lower()}%"
        pattern = (like_term, like_term, like_term)

    clauses = [col_predicate]
    params: list = list(pattern)

    if args.since:
        clauses.append("captured_at >= ?")
        params.append(args.since)
    if args.until:
        clauses.append("captured_at < ?")
        params.append(args.until)
    if args.session is not None:
        clauses.append("session_id = ?")
        params.append(args.session)

    where = " AND ".join(clauses)
    sql = (
        "SELECT id, captured_at, session_id, source_app, source_tier, "
        "  title, content_text, delta_text "
        f"FROM raw_events WHERE {where} "
        "ORDER BY captured_at "
        "LIMIT ?"
    )
    params.append(args.limit)

    conn = _open_connection(_DB_PATH)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"No matches for {term!r}.")
        return

    needle_lower = term if args.case_sensitive else term.lower()
    print(f"{len(rows)} match(es) for {term!r}:\n")

    for (row_id, captured_at, session_id, source_app, source_tier,
         title, content_text, delta_text) in rows:
        # Identify which columns matched so an auditor can tell whether
        # the hit was in the visible content or hidden in a delta.
        matched_in: list[str] = []
        for col_name, col_value in (
            ("content_text", content_text),
            ("delta_text", delta_text),
            ("title", title),
        ):
            if col_value:
                haystack = col_value if args.case_sensitive else col_value.lower()
                if needle_lower in haystack:
                    matched_in.append(col_name)

        # Pick the first match'd column with non-empty content for the
        # snippet — content_text wins ties because it's the canonical
        # surface; delta_text is the most common audit-blind-spot hit.
        snippet_source = ""
        snippet_col = ""
        for col_name, col_value in (
            ("content_text", content_text),
            ("delta_text", delta_text),
            ("title", title),
        ):
            if col_name in matched_in and col_value:
                snippet_source = col_value
                snippet_col = col_name
                break

        snip = _snippet(snippet_source, needle_lower, args.context)

        header = (
            f"id={row_id}  {captured_at}  "
            f"session={session_id if session_id is not None else '-'}  "
            f"{source_app or '-'}/{source_tier or '-'}  "
            f"title={title!r}"
        )
        print(header)
        print(f"  matched in: {', '.join(matched_in)}  (snippet from {snippet_col})")
        print(f"  {snip}")
        print()
