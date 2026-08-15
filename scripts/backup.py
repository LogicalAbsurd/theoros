"""Theoros nightly backup.

Snapshots the local SQLite database via the SQLite Backup API (safe on a
live, WAL-mode database with the capture worker holding write locks —
never `cp` on the live file, which would produce a half-committed page
image), bundles the snapshot with memory/reflections/, encrypts the
bundle with age using the recipient file at
~/.config/theoros/backup-age.recipient, and writes the result to two
destinations:

    1. Local NTFS HDD at /mnt/backup/theoros/ (if mounted).
    2. rclone remote gdrive:theoros-backups/.

Both sides are pruned to the newest 14 files after each run.  If the
HDD is not mounted (drive powered off, cable unplugged, etc.) the run
logs a warning and continues with cloud-only rather than failing —
false-absence of the HDD is a normal condition, not an error.  A cloud
failure IS an error and exits non-zero, so systemd surfaces it in
`systemctl --user status theoros-backup`.

Restore path (out-of-band, documented in docs/backup-setup.md):

    age -d -i ~/.config/theoros/backup-age.key \\
        theoros-backup-YYYY-MM-DD.tar.gz.age \\
        | tar xz -C restore-dir/

Run:
    .venv/bin/python scripts/backup.py
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path

# scripts/backup.py lives at repo_root/scripts/, so parents[1] is the
# repo root.  Used to resolve relative paths (DB, reflections dir) in a
# way that matches how the worker modules resolve them.
REPO_ROOT = Path(__file__).resolve().parents[1]

log = logging.getLogger("theoros.backup")

# Filename convention: date-stamped so pruning-by-name is deterministic
# regardless of rclone mtime behavior (rclone tends to rewrite mtimes on
# copy, so sorting by mtime on the cloud side would be unreliable).
BACKUP_NAME_RE = re.compile(r"^theoros-backup-(\d{4}-\d{2}-\d{2})\.tar\.gz\.age$")


def _env_path(name: str, default: str) -> Path:
    """Read a filesystem-path env var, resolving ~ and relative-to-repo."""
    raw = os.environ.get(name, default)
    p = Path(raw).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


class Settings:
    """Configuration read from environment variables at startup."""

    def __init__(self) -> None:
        # Source database.  Same default as the workers.
        self.db_path = _env_path("THEOROS_DB_PATH", "memory/system/theoros.db")

        # Reflections directory to bundle alongside the DB.
        self.reflections_dir = _env_path(
            "THEOROS_REFLECTIONS_DIR", "memory/reflections"
        )

        # Age recipient (public key) file.  We deliberately do NOT read
        # the private key here — encryption only needs the recipient,
        # and keeping the private key out of this script's file-access
        # graph reduces the blast radius if the script or its runtime
        # is ever compromised.
        self.age_recipient_file = _env_path(
            "THEOROS_BACKUP_AGE_RECIPIENT_FILE",
            "~/.config/theoros/backup-age.recipient",
        )

        # Local HDD destination.  Parent directory must be a real mount
        # point — if it isn't, the run treats the HDD as absent (which
        # is not an error).
        self.hdd_dir = _env_path("THEOROS_BACKUP_HDD_DIR", "/mnt/backup/theoros")

        # The parent that must be an active mount for the HDD side to
        # be considered available.  Default derives from hdd_dir's
        # parent so setting THEOROS_BACKUP_HDD_DIR alone works.
        mount = os.environ.get("THEOROS_BACKUP_HDD_MOUNTPOINT")
        self.hdd_mountpoint = Path(mount).expanduser() if mount else self.hdd_dir.parent

        # rclone remote + subpath.  Trailing slash normalized off; the
        # script adds slashes where it needs them.
        self.rclone_remote = _env_str(
            "THEOROS_BACKUP_RCLONE_REMOTE", "gdrive:theoros-backups"
        ).rstrip("/")

        # Retention on both sides, in files.  Filename-date-based, so
        # this is effectively days (one backup per day).
        self.retention = _env_int("THEOROS_BACKUP_RETENTION", 14)


# ---------------------------------------------------------------------------
# DB snapshot
# ---------------------------------------------------------------------------

def _snapshot_db(source_path: Path, dest_path: Path) -> None:
    """Copy the live SQLite database to dest_path via the Backup API.

    The Backup API cooperates with WAL-mode writers: pages are copied
    in a consistent order and the destination file is transactionally
    equivalent to a point-in-time snapshot of the source, even while
    the capture worker is writing.  Plain `cp` on a WAL-mode DB can
    capture a torn write — do not use it.
    """
    # Open source read-only via URI so a concurrent writer can't
    # accidentally be blocked by a schema-lock upgrade.
    src_uri = f"file:{source_path}?mode=ro"
    src = sqlite3.connect(src_uri, uri=True)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            # pages=-1 copies the whole DB in one shot (no yielding to
            # writers).  For a 4 GB DB this takes seconds and simplifies
            # error handling; if this ever becomes a problem for
            # concurrent writers we can move to pages=1000 with a small
            # sleep.
            src.backup(dst, pages=-1)
        finally:
            dst.close()
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Bundle + encrypt
# ---------------------------------------------------------------------------

def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Exclude files that shouldn't be in the backup bundle.

    - `*.buggy-backup` are hand-preserved bad reflection outputs — kept
      locally for reference but not something to propagate into every
      nightly snapshot.
    - `.gitkeep` is a repo-structure marker, not data.
    """
    name = os.path.basename(info.name)
    if name.endswith(".buggy-backup"):
        return None
    if name == ".gitkeep":
        return None
    return info


def _build_and_encrypt(
    db_snapshot: Path,
    reflections_dir: Path,
    recipient_file: Path,
    out_path: Path,
) -> None:
    """Stream a tar.gz of the DB snapshot + reflections into age.

    Streams via a pipe so we never write the plaintext tarball to disk —
    tar output goes straight into age's stdin, and only the encrypted
    file ever lands on the filesystem.  Reduces peak disk use by ~half
    on a 4 GB DB and shortens the window during which unencrypted
    session data exists outside the source DB.
    """
    age_proc = subprocess.Popen(
        ["age", "-R", str(recipient_file), "-o", str(out_path)],
        stdin=subprocess.PIPE,
    )
    try:
        assert age_proc.stdin is not None
        with tarfile.open(fileobj=age_proc.stdin, mode="w:gz") as tar:
            # DB snapshot goes at a stable path inside the archive so
            # restore is scriptable.
            tar.add(db_snapshot, arcname="theoros.db")

            # Reflections dir may or may not exist depending on phase.
            # If it doesn't, that's fine — the DB alone is still worth
            # backing up.
            if reflections_dir.exists():
                tar.add(
                    reflections_dir,
                    arcname="reflections",
                    filter=_tar_filter,
                )
            else:
                log.warning(
                    "reflections dir not found at %s; "
                    "bundle will contain DB only",
                    reflections_dir,
                )
        age_proc.stdin.close()
    except Exception:
        age_proc.kill()
        age_proc.wait()
        raise

    rc = age_proc.wait()
    if rc != 0:
        raise RuntimeError(f"age exited with status {rc}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------

def _hdd_available(mountpoint: Path) -> bool:
    """True if `mountpoint` is a currently active mount point.

    We check ismount() rather than just exists() so an empty stub
    directory left behind after an unmount doesn't get treated as the
    HDD being available.  Writing into a stub would silently land on
    root instead of the HDD.
    """
    return mountpoint.is_dir() and os.path.ismount(mountpoint)


def _write_to_hdd(source: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    # Copy rather than move so the source stays available for the
    # cloud upload step.  copy2 preserves mtime, which is nice for
    # `ls -lt` even though we prune by filename.
    shutil.copy2(source, dest)
    return dest


def _write_to_rclone(source: Path, remote: str) -> None:
    """Upload the encrypted bundle to the rclone remote.

    Uses `copyto` with an explicit dest filename so rclone doesn't
    inherit any weird path semantics from the source path.  Timeouts
    are conservative — a 4 GB DB compressed can be 1-2 GB, and Google
    Drive uploads on typical residential upload speeds take 5-30
    minutes.  systemd will interrupt us if we exceed the timer's
    RuntimeMaxSec, so no need to layer another timeout here.
    """
    dest = f"{remote}/{source.name}"
    log.info("rclone copyto %s -> %s", source.name, dest)
    subprocess.run(
        ["rclone", "copyto", str(source), dest, "--stats-one-line", "--stats=1m"],
        check=True,
    )


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def _parse_backup_date(name: str) -> str | None:
    m = BACKUP_NAME_RE.match(name)
    return m.group(1) if m else None


def _prune_hdd(dir_path: Path, keep: int) -> None:
    if not dir_path.is_dir():
        return
    dated = [
        (d, p)
        for p in dir_path.iterdir()
        if p.is_file() and (d := _parse_backup_date(p.name)) is not None
    ]
    # Newest first, keep the first `keep`.
    dated.sort(key=lambda t: t[0], reverse=True)
    for _, p in dated[keep:]:
        log.info("hdd prune: %s", p.name)
        p.unlink()


def _prune_rclone(remote: str, keep: int) -> None:
    """List and prune old backups on the rclone remote.

    Uses `rclone lsf` (bare filenames, one per line) rather than
    `lsjson` — no metadata is needed beyond the name, which encodes
    the date.
    """
    result = subprocess.run(
        ["rclone", "lsf", remote + "/"],
        check=True,
        capture_output=True,
        text=True,
    )
    dated = []
    for line in result.stdout.splitlines():
        name = line.strip().rstrip("/")
        d = _parse_backup_date(name)
        if d is not None:
            dated.append((d, name))
    dated.sort(key=lambda t: t[0], reverse=True)
    for _, name in dated[keep:]:
        log.info("rclone prune: %s", name)
        subprocess.run(
            ["rclone", "delete", f"{remote}/{name}"],
            check=True,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings()

    # Preflight: fail loudly if the source DB or recipient file are
    # missing.  These are configuration errors, not runtime conditions,
    # and we want them visible before we spend time snapshotting.
    if not settings.db_path.exists():
        log.error("source DB not found: %s", settings.db_path)
        return 2
    if not settings.age_recipient_file.exists():
        log.error(
            "age recipient file not found: %s "
            "(create it per docs/backup-setup.md)",
            settings.age_recipient_file,
        )
        return 2

    today = date.today().isoformat()
    bundle_name = f"theoros-backup-{today}.tar.gz.age"

    cloud_ok = False
    hdd_ok = False
    hdd_attempted = False

    # Scratch dir lives on the real disk under memory/system/, not
    # under /tmp.  Under systemd's PrivateTmp=true, /tmp is a fresh
    # tmpfs — writing the ~4 GB DB snapshot there would pin it in RAM
    # (observed 4.9 G RSS + 1.8 G swap on the first live run).
    # memory/system/ is on the same filesystem as the source DB, so
    # the initial snapshot could even use a reflink on btrfs/xfs
    # eventually.  Created with 0700 so no other user could read the
    # plaintext snapshot during the run.
    scratch_root = settings.db_path.parent / ".backup-tmp"
    scratch_root.mkdir(mode=0o700, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="theoros-backup-", dir=str(scratch_root)
    ) as tmp:
        tmp_dir = Path(tmp)
        db_snap = tmp_dir / "theoros.db"
        bundle_path = tmp_dir / bundle_name

        log.info("snapshot DB: %s -> %s", settings.db_path, db_snap)
        _snapshot_db(settings.db_path, db_snap)
        snap_size = db_snap.stat().st_size
        log.info("snapshot size: %.1f MiB", snap_size / (1024 * 1024))

        log.info("bundle + encrypt: %s", bundle_path.name)
        _build_and_encrypt(
            db_snap,
            settings.reflections_dir,
            settings.age_recipient_file,
            bundle_path,
        )
        bundle_size = bundle_path.stat().st_size
        digest = _sha256(bundle_path)
        log.info(
            "bundle: %.1f MiB, sha256=%s",
            bundle_size / (1024 * 1024),
            digest,
        )

        # HDD side.  Absence is a warning, not an error.
        if _hdd_available(settings.hdd_mountpoint):
            hdd_attempted = True
            try:
                dest = _write_to_hdd(bundle_path, settings.hdd_dir)
                log.info("hdd write ok: %s", dest)
                _prune_hdd(settings.hdd_dir, settings.retention)
                hdd_ok = True
            except Exception:
                log.exception("hdd write failed")
        else:
            log.warning(
                "hdd not mounted at %s; skipping HDD copy, continuing cloud-only",
                settings.hdd_mountpoint,
            )

        # Cloud side.  Failure is an error.
        try:
            _write_to_rclone(bundle_path, settings.rclone_remote)
            _prune_rclone(settings.rclone_remote, settings.retention)
            cloud_ok = True
        except Exception:
            log.exception("rclone upload or prune failed")

    # Exit code policy:
    #   0 — cloud succeeded (HDD may have failed or been absent).
    #   1 — cloud failed (regardless of HDD).
    #   2 — configuration error (handled above).
    if not cloud_ok:
        log.error("cloud backup failed; exiting non-zero")
        return 1

    if hdd_attempted and not hdd_ok:
        # Cloud ok, HDD present but write failed — surface it in the log
        # but don't fail the run.  The point of two destinations is
        # exactly this: one can fail and we still have coverage.
        log.warning("cloud backup ok; HDD copy failed (see above)")

    log.info("backup complete: cloud=ok hdd=%s", "ok" if hdd_ok else "skipped/failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
