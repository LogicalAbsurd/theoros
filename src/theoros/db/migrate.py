"""Apply unrun SQL migrations to the local Theoros SQLite database.

Run as a module from the repo root:

    python -m theoros.db.migrate

Migrations live in src/theoros/db/migrations/ as .sql files named
NNN_description.sql. They are applied in filename sort order. Each
applied migration is recorded in a schema_migrations table inside the
same database, keyed by filename, with a SHA-256 of the file's bytes.
On subsequent runs, already-applied migrations are verified against
their recorded hash — any drift raises rather than silently re-applying,
so a modified-after-the-fact migration is caught early.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# -----------------------------------------------------------------------------
# Paths
#
# migrate.py lives at src/theoros/db/migrate.py, so parents[3] resolves to
# the repo root regardless of the process's current working directory.
# This matters because `python -m theoros.db.migrate` and a future
# systemd unit will both invoke the runner from different CWDs.
# -----------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DB_PATH = REPO_ROOT / "memory" / "system" / "theoros.db"


SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    sha256     TEXT NOT NULL
)
"""


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    """Create schema_migrations if it doesn't already exist."""
    conn.execute(SCHEMA_MIGRATIONS_DDL)


def _applied_migrations(conn: sqlite3.Connection) -> dict[str, str]:
    """Return a {filename: sha256} map of migrations already recorded."""
    cursor = conn.execute("SELECT filename, sha256 FROM schema_migrations")
    return {row[0]: row[1] for row in cursor.fetchall()}


def _discover_migrations(migrations_dir: Path) -> list[Path]:
    """Return every .sql file in migrations_dir, sorted by filename."""
    return sorted(migrations_dir.glob("*.sql"))


def _file_sha256(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of a file's raw bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now_iso() -> str:
    """Return the current time as ISO-8601 UTC with millisecond precision.

    Format matches what the schema comments use, e.g.
    '2026-04-21T22:15:30.123Z'. Using timezone-aware datetime.now(tz=UTC)
    rather than the deprecated naive utcnow().
    """
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def apply_migrations(
    db_path: Path = DB_PATH,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> None:
    """Bring the SQLite database at db_path up to date with the migrations on disk.

    Unrun migrations are applied in filename sort order. Already-run
    migrations are verified against their recorded SHA-256; a mismatch
    raises RuntimeError rather than re-applying or silently continuing.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None puts the connection in autocommit mode. This
    # matters because migration 002 contains PRAGMA foreign_keys = OFF/ON
    # (PRAGMAs must run outside a transaction) alongside an explicit
    # BEGIN/COMMIT pair. In the default implicit-transaction mode, sqlite3
    # would wrap those PRAGMAs in a transaction and break them. With
    # autocommit, migrations own their transactional semantics; the runner
    # wraps only the schema_migrations INSERT, which is a single statement
    # and therefore atomic on its own.
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_tracking_table(conn)
        applied = _applied_migrations(conn)

        for migration in _discover_migrations(migrations_dir):
            filename = migration.name
            digest = _file_sha256(migration)

            if filename in applied:
                recorded = applied[filename]
                if recorded != digest:
                    raise RuntimeError(
                        f"migration {filename} has been modified since it was "
                        f"applied: recorded sha256 {recorded}, current "
                        f"sha256 {digest}. Revert the file or write a new "
                        f"migration rather than editing an applied one."
                    )
                print(f"already applied {filename}")
                continue

            print(f"applying {filename}")
            # executescript handles multi-statement SQL, including the
            # explicit BEGIN/COMMIT and PRAGMA lines that migration 002
            # contains. If it raises, the INSERT below never runs, so the
            # migration is not marked applied — the next invocation will
            # retry. The tradeoff: if executescript committed part of a
            # migration before failing (possible for migrations without
            # their own BEGIN/COMMIT), the database can be left in a
            # partial state. Migrations that need atomicity should wrap
            # themselves in BEGIN/COMMIT; 002 already does.
            sql = migration.read_text(encoding="utf-8")
            conn.executescript(sql)

            conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at, sha256) "
                "VALUES (?, ?, ?)",
                (filename, _utc_now_iso(), digest),
            )
    finally:
        conn.close()


def main() -> int:
    """Module entrypoint. Returns a shell exit code."""
    try:
        apply_migrations()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
