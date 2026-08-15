"""SQLite write layer for the capture service.

Uses the stdlib sqlite3 module directly — no ORM — matching the approach
in theoros.db.migrate. The raw_events table must already exist (created
by running `python -m theoros.db.migrate` before starting the service).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .config import CaptureSettings
from .models import CaptureEvent
from .redact import redact_credentials

# Tiers eligible for server-side composition-collapse.  Browser is
# excluded because it has its own chain logic keyed on tab_session_id.
# OCR is excluded because it re-photographs on-screen text that isn't
# being composed — treating those recaptures as extensions would create
# spurious chains full of identical prefixes.  window_meta carries no
# content_text worth chaining.
_COMPOSITION_TIERS = frozenset({"atspi", "mobile"})


@lru_cache(maxsize=1)
def _capture_settings() -> CaptureSettings:
    """Return a cached CaptureSettings so composition thresholds are read
    from env/.env once per process rather than reconstructing on every
    insert."""
    return CaptureSettings()

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return the current time as ISO-8601 UTC with millisecond precision.

    Matches the format used by migrate.py and the schema comments,
    e.g. '2026-04-21T14:32:17.123Z'.
    """
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _content_hash(text: str | None) -> str:
    """SHA-256 hex digest of the content text.

    When content_text is None (metadata-only OCR events), we hash the
    empty string. The schema requires content_hash NOT NULL, and using
    a deterministic value for the null-content case means two empty
    captures still dedup correctly downstream.
    """
    payload = (text or "").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def open_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and foreign keys enabled.

    WAL (write-ahead logging) lets readers proceed without blocking
    writers and vice versa — important once the normalization pipeline
    reads raw_events while the capture service is still writing them.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def check_raw_events_table(db_path: Path) -> None:
    """Verify that the raw_events table exists, raising if it doesn't.

    Called at startup so the service fails loudly with a clear message
    rather than crashing on the first insert with a confusing
    'no such table' error.
    """
    conn = open_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='raw_events'"
        )
        if cursor.fetchone() is None:
            raise RuntimeError(
                f"raw_events table not found in {db_path}. "
                f"Run `python -m theoros.db.migrate` to create the schema before "
                f"starting the capture service."
            )
    finally:
        conn.close()


def check_for_duplicate(
    db_path: Path, tab_session_id: str | None, content_hash: str
) -> tuple[int, str] | None:
    """Return (id, received_at) of an existing row with the same tab_session_id
    and content_hash, or None if no duplicate exists.

    Dedup requires a tab_session_id — without one we can't scope the check
    to a particular tab session, so we skip dedup entirely.
    """
    if not tab_session_id:
        return None

    conn = open_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, received_at FROM raw_events "
            "WHERE raw_metadata->>'tab_session_id' = ? AND content_hash = ? "
            "LIMIT 1",
            (tab_session_id, content_hash),
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


def reconstruct_chain_text(
    conn: sqlite3.Connection, chain_id: str, up_to_position: int
) -> str:
    """Reconstruct full text at a given chain position by walking from the root.

    Handles both old-style (strict-prefix, lcp_length NULL) and new-style
    (LCP-based, lcp_length set) continuation rows:

      - Root (position 1): content_text or delta_text, whichever is non-NULL.
      - Continuation with lcp_length NULL (old-style): append delta_text to
        the accumulated text.  This preserves backward compatibility with
        rows written before migration 005.
      - Continuation with lcp_length set (new-style): truncate accumulated
        text to lcp_length characters, then append delta_text.

    Public because downstream consumers (normalization, retrieval) will need
    chain reconstruction too.
    """
    cursor = conn.execute(
        "SELECT chain_position, content_text, delta_text, lcp_length "
        "FROM raw_events "
        "WHERE chain_id = ? AND chain_position <= ? "
        "ORDER BY chain_position",
        (chain_id, up_to_position),
    )
    accumulated = ""
    for pos, content_text, delta_text, lcp_length in cursor:
        if pos == 1:
            # Root: delta_text or content_text, whichever is non-NULL.
            accumulated = (delta_text if delta_text is not None else content_text) or ""
        elif lcp_length is not None:
            # New-style (LCP): truncate to the common prefix, append delta.
            accumulated = accumulated[:lcp_length] + (delta_text or "")
        else:
            # Old-style (strict-prefix): delta is a pure suffix — append.
            accumulated += (delta_text or "")
    return accumulated


def find_chain_predecessor(
    db_path: Path, tab_session_id: str, source_identifier: str
) -> dict[str, object] | None:
    """Find the most recent raw_events row for a (tab_session_id, source_identifier) pair.

    Returns None if no prior row matches or the predecessor has no chain_id.
    Otherwise returns a dict:
        prior_id:             int        — row id
        prior_chain_id:       str        — UUID of the chain
        prior_chain_position: int        — position within the chain
        prior_full_text:      str        — reconstructed full text at that position

    Under the LCP model the caller always continues the predecessor's chain
    (no breakpoints).  Empty-text predecessors are legitimate — LCP will be 0
    and the new content becomes the entire delta.
    """
    conn = open_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, chain_id, chain_position, is_chain_root, content_text "
            "FROM raw_events "
            "WHERE raw_metadata->>'tab_session_id' = ? "
            "  AND source_identifier = ? "
            "ORDER BY id DESC LIMIT 1",
            (tab_session_id, source_identifier),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        prior_id, chain_id, chain_position, is_chain_root, content_text = row

        # Can't extend a chain that has no chain_id (shouldn't happen in
        # practice, but defensive against NULL from future edge cases).
        if chain_id is None:
            return None

        # Get the full text at this position.
        if is_chain_root and content_text is not None:
            full_text = content_text
        else:
            full_text = reconstruct_chain_text(conn, chain_id, chain_position)

        return {
            "prior_id": prior_id,
            "prior_chain_id": chain_id,
            "prior_chain_position": chain_position,
            "prior_full_text": full_text,
        }
    finally:
        conn.close()


def find_composition_predecessor(
    db_path: Path,
    source_tier: str,
    source_app: str,
    source_identifier: str,
    freshness_seconds: int,
) -> dict[str, object] | None:
    """Find the most recent same-tier same-app-and-identifier capture within
    the freshness window.  Analog of find_chain_predecessor for tiers that
    don't have a tab_session_id (atspi, mobile).

    Returns the same shape as find_chain_predecessor, or None if the last
    matching capture is older than freshness_seconds (composition burst
    is over) or if nothing matches at all.
    """
    threshold_iso = (
        (datetime.now(tz=timezone.utc) - timedelta(seconds=freshness_seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    conn = open_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, chain_id, chain_position, is_chain_root, content_text "
            "FROM raw_events "
            "WHERE source_tier = ? "
            "  AND source_app = ? "
            "  AND source_identifier = ? "
            "  AND received_at >= ? "
            "ORDER BY id DESC LIMIT 1",
            (source_tier, source_app, source_identifier, threshold_iso),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        prior_id, chain_id, chain_position, is_chain_root, content_text = row

        if chain_id is None:
            return None

        if is_chain_root and content_text is not None:
            full_text = content_text
        else:
            full_text = reconstruct_chain_text(conn, chain_id, chain_position)

        return {
            "prior_id": prior_id,
            "prior_chain_id": chain_id,
            "prior_chain_position": chain_position,
            "prior_full_text": full_text,
        }
    finally:
        conn.close()


def _longest_common_prefix(a: str, b: str) -> int:
    """Return the length of the longest common prefix between two strings."""
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return i
    return limit


def insert_raw_event(
    db_path: Path, event: CaptureEvent
) -> tuple[int, str, bool, str, str | None, int | None, int | None]:
    """Insert a capture event into raw_events and return
    (row_id, received_at, was_duplicate, content_hash, chain_id, chain_position,
    lcp_length).

    Before anything else, content_text is scanned for credential patterns
    (API keys, tokens, card numbers, SSNs) and matches are replaced with
    [REDACTED:<pattern_name>].  The redacted text is used for content_hash,
    content_length, chain LCP computation, and the stored content — secrets
    never land on disk.

    If the same content_hash already exists for this tab_session_id, the
    insert is skipped and the existing row's id/received_at are returned
    with was_duplicate=True.  chain_id, chain_position, and lcp_length are
    None for duplicates (no chain logic runs).

    Chain logic (non-duplicate path):
      - No prior capture for this (tab_session_id, source_identifier) → new chain.
      - Prior exists → continuation (always; no breakpoints in the LCP model).
    Chain identity follows (tab_session_id, source_identifier).  A chain ends
    only when the tab session resets or the URL changes.

    Opens a fresh connection per request. SQLite handles this fine at our
    volume — connection pooling would add complexity for no measurable gain
    in a single-user local service.
    """
    # --- Redaction (runs before dedup so the hash reflects redacted content) ---
    redacted_text, applied_patterns = redact_credentials(event.content_text or "")
    # Preserve the None vs "" distinction: if the client sent no text, keep None.
    if event.content_text is None:
        redacted_text = None

    if applied_patterns:
        tab_session_short = ((event.raw_metadata or {}).get("tab_session_id") or "")[:8]
        source_short = (event.source_identifier or "")[:80]
        logger.info(
            "redacted: patterns=%s in tab_session=%s source=%s",
            ",".join(applied_patterns), tab_session_short, source_short,
        )

    content_hash = _content_hash(redacted_text)

    # Dedup check runs after redaction so the hash matches stored (redacted) content.
    tab_session_id = (event.raw_metadata or {}).get("tab_session_id")
    existing = check_for_duplicate(db_path, tab_session_id, content_hash)
    if existing is not None:
        return existing[0], existing[1], True, content_hash, None, None, None

    received_at = _utc_now_iso()
    content_length = len(redacted_text) if redacted_text else 0

    # Format captured_at the same way as received_at — ISO-8601 UTC with
    # millisecond precision.  The client sends a datetime; we normalize it
    # to our string format for storage consistency.
    captured_at = (
        event.captured_at.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    raw_metadata_json = json.dumps(event.raw_metadata) if event.raw_metadata else None

    # --- Chain logic ---
    # Default: new chain of length 1 (single-shot capture).  Used when chain
    # logic is skipped — no content, no tab_session_id, or no source_identifier.
    chain_id = str(uuid.uuid4())
    chain_position = 1
    is_chain_root = 1
    content_text_to_store = redacted_text
    delta_text: str | None = None
    lcp_length: int | None = None

    # Composition tiers (atspi, mobile) are routed to the composition path
    # even when they carry a tab_session_id — mobile's synthetic
    # tab_session_id would otherwise pull them into browser-chain logic,
    # which has no extension-threshold gate and would chain across
    # unrelated bursts.  Browser stays on its own chain path below.
    if (
        event.source_tier not in _COMPOSITION_TIERS
        and redacted_text is not None
        and tab_session_id
        and event.source_identifier
    ):
        predecessor = find_chain_predecessor(
            db_path, tab_session_id, event.source_identifier
        )

        if predecessor is not None:
            # Continuation — always, under the LCP model.  Chain identity is
            # (tab_session_id, source_identifier); no breakpoints.
            prior_full_text: str = predecessor["prior_full_text"]
            chain_id = predecessor["prior_chain_id"]
            chain_position = predecessor["prior_chain_position"] + 1
            is_chain_root = 0
            content_text_to_store = None
            lcp_length = _longest_common_prefix(prior_full_text, redacted_text)
            delta_text = redacted_text[lcp_length:]

    elif (
        event.source_tier in _COMPOSITION_TIERS
        and redacted_text is not None
        and event.source_app
        and event.source_identifier
    ):
        # Composition-collapse path.  Modeled on the browser chain above
        # but keyed on (source_tier, source_app, source_identifier) with a
        # freshness window standing in for the missing tab_session_id.
        # Unlike browser, the chain breaks if the new capture doesn't
        # substantially extend the prior one — a cleared field or a shift
        # to unrelated content is not composition, it's a new session.
        settings = _capture_settings()
        predecessor = find_composition_predecessor(
            db_path,
            event.source_tier,
            event.source_app,
            event.source_identifier,
            settings.theoros_composition_freshness_seconds,
        )

        if predecessor is not None:
            prior_full_text = predecessor["prior_full_text"]
            candidate_lcp = _longest_common_prefix(prior_full_text, redacted_text)
            prior_len = len(prior_full_text)
            # Extension test: the new text must preserve at least
            # lcp_threshold of the prior text as a prefix.  A prior of
            # length 0 is trivially extended by anything.  Below the
            # threshold we fall through to the single-shot default and
            # start a fresh chain.
            threshold = settings.theoros_composition_lcp_threshold
            is_extension = prior_len == 0 or candidate_lcp >= threshold * prior_len

            if is_extension:
                chain_id = predecessor["prior_chain_id"]
                chain_position = predecessor["prior_chain_position"] + 1
                is_chain_root = 0
                content_text_to_store = None
                lcp_length = candidate_lcp
                delta_text = redacted_text[lcp_length:]

    conn = open_connection(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO raw_events (
                captured_at, received_at, source_tier, source_app,
                source_identifier, title, content_text, content_length,
                content_hash, chain_id, chain_position, is_chain_root,
                delta_text, lcp_length, confidence, redactions_applied,
                raw_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at,
                received_at,
                event.source_tier,
                event.source_app,
                event.source_identifier,
                event.title,
                content_text_to_store,
                content_length,
                content_hash,
                chain_id,
                chain_position,
                is_chain_root,
                delta_text,
                lcp_length,
                event.confidence,
                json.dumps(applied_patterns),
                raw_metadata_json,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
        assert row_id is not None  # AUTOINCREMENT guarantees this.
    finally:
        conn.close()

    # --- Logging ---
    short_chain = chain_id[:8]
    if chain_position > 1:
        logger.info(
            "captured: chain=%s position=%d root=0 lcp=%d delta_length=%d "
            "id=%d tier=%s app=%s",
            short_chain, chain_position, lcp_length,
            len(delta_text or ""),
            row_id, event.source_tier, event.source_app,
        )
    else:
        logger.info(
            "captured: chain=%s position=1 root=1 "
            "id=%d tier=%s app=%s length=%d",
            short_chain,
            row_id, event.source_tier, event.source_app, content_length,
        )

    return row_id, received_at, False, content_hash, chain_id, chain_position, lcp_length


def get_event_count(db_path: Path) -> int:
    """Return the total number of rows in raw_events."""
    conn = open_connection(db_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM raw_events")
        return cursor.fetchone()[0]
    finally:
        conn.close()


def get_last_capture_at(db_path: Path) -> str | None:
    """Return the most recent captured_at timestamp, or None if the table is empty."""
    conn = open_connection(db_path)
    try:
        cursor = conn.execute("SELECT MAX(captured_at) FROM raw_events")
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def check_db_health(db_path: Path) -> str | None:
    """Return None if the database is reachable, or an error string if not."""
    try:
        conn = open_connection(db_path)
        try:
            conn.execute("SELECT 1 FROM raw_events LIMIT 1")
        finally:
            conn.close()
        return None
    except Exception as exc:
        return str(exc)

def list_url_exclusions(db_path: Path) -> tuple[list[str], list[str]]:
    """Return all URL exclusions as two lists: (domains, path_prefixes).

    Splits the response shape here rather than in the route handler so
    the route stays a thin pass-through. Sorted alphabetically for
    deterministic output — helpful for testing and for diffing between
    clients that cache the list locally.
    """
    conn = open_connection(db_path)
    try:
        cur = conn.execute(
            "SELECT kind, value FROM url_exclusions ORDER BY kind, value"
        )
        domains: list[str] = []
        path_prefixes: list[str] = []
        for kind, value in cur:
            if kind == "domain":
                domains.append(value)
            else:
                path_prefixes.append(value)
        return domains, path_prefixes
    finally:
        conn.close()


def insert_url_exclusion(db_path: Path, kind: str, value: str) -> bool:
    """Insert a URL exclusion. Returns True if a new row was created.

    Idempotent: if (kind, value) already exists, the UNIQUE constraint
    fires, we swallow the IntegrityError, and return False. The
    extension's one-time migration of chrome.storage.local data depends
    on this behavior for safe retry.
    """
    conn = open_connection(db_path)
    try:
        try:
            conn.execute(
                "INSERT INTO url_exclusions (kind, value, created_at) VALUES (?, ?, ?)",
                (kind, value, _utc_now_iso()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # UNIQUE (kind, value) violation — row already exists.
            return False
    finally:
        conn.close()


def delete_url_exclusion(db_path: Path, kind: str, value: str) -> bool:
    """Delete a URL exclusion. Returns True if a row was removed.

    Idempotent: deleting a non-existent row returns False rather than
    raising, so clients can safely retry DELETE on network failure.
    """
    conn = open_connection(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM url_exclusions WHERE kind = ? AND value = ?",
            (kind, value),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
