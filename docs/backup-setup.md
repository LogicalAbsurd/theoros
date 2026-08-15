# Theoros backup — setup, operation, restore

Nightly encrypted snapshot of the local SQLite database and reflections
tree, written to two destinations:

- Local NTFS HDD at `/mnt/backup/theoros/` (skipped with a warning if
  the disk isn't mounted — false-absence is a normal condition).
- rclone remote `gdrive:theoros-backups/`.

Both sides retain the newest 14 files and prune older automatically.

The DB snapshot is taken via SQLite's Backup API — safe against a live
capture worker holding WAL-mode write locks — and streamed straight
into `age` so the plaintext tarball never lands on disk.

## One-time setup

### Age key

Generate a keypair, lock down the private key, extract the recipient
into its own file:

```bash
mkdir -p ~/.config/theoros && chmod 700 ~/.config/theoros
age-keygen -o ~/.config/theoros/backup-age.key
chmod 600 ~/.config/theoros/backup-age.key
# Copy the "Public key: age1..." line printed by age-keygen into:
printf 'age1YOUR_PUBLIC_KEY_HERE\n' > ~/.config/theoros/backup-age.recipient
```

Verify round-trip:

```bash
echo 'smoke-test' | age -R ~/.config/theoros/backup-age.recipient > /tmp/smoke.age
age -d -i ~/.config/theoros/backup-age.key /tmp/smoke.age  # should print smoke-test
rm /tmp/smoke.age
```

**Back up the private key off-box.** If this machine's disk dies and
the private key exists only here, every encrypted backup on the HDD and
in the cloud becomes permanently unreadable. Recommended:

1. Password manager entry with the full contents of
   `~/.config/theoros/backup-age.key` (both the comment lines and the
   `AGE-SECRET-KEY-1...` line).
2. Paper copy of just the `AGE-SECRET-KEY-1...` line in a fireproof
   location. Slower to restore, immune to every digital failure mode.

### rclone remote

Confirm the remote exists and points at the right Google Drive account:

```bash
rclone lsd gdrive:
```

The script writes to `gdrive:theoros-backups/`; that directory is
created on first upload if absent.

### Install the systemd units

```bash
ln -s ~/dev/projects/ai/theoros/deploy/systemd/theoros-backup.service \
      ~/.config/systemd/user/theoros-backup.service
ln -s ~/dev/projects/ai/theoros/deploy/systemd/theoros-backup.timer \
      ~/.config/systemd/user/theoros-backup.timer
systemctl --user daemon-reload
systemctl --user enable --now theoros-backup.timer
```

`enable --now` on the timer arms it for 03:30 and starts the timer
immediately (without triggering an immediate backup — the timer only
schedules).

## Configuration

Environment variables read at runtime (all optional, defaults shown):

| Variable | Default | Purpose |
|---|---|---|
| `THEOROS_DB_PATH` | `memory/system/theoros.db` | Source DB (relative resolves against repo root) |
| `THEOROS_REFLECTIONS_DIR` | `memory/reflections` | Reflections tree bundled with the DB |
| `THEOROS_BACKUP_AGE_RECIPIENT_FILE` | `~/.config/theoros/backup-age.recipient` | Public key file for age encryption |
| `THEOROS_BACKUP_HDD_DIR` | `/mnt/backup/theoros` | Local HDD destination |
| `THEOROS_BACKUP_HDD_MOUNTPOINT` | parent of `HDD_DIR` | Mount point checked with `ismount()` before HDD write |
| `THEOROS_BACKUP_RCLONE_REMOTE` | `gdrive:theoros-backups` | rclone remote + path (no trailing slash) |
| `THEOROS_BACKUP_RETENTION` | `14` | Newest N files kept on each side |

## Operation

Manual run (safe, uses the same code path as the timer):

```bash
systemctl --user start theoros-backup.service
# or, without systemd wrapping:
.venv/bin/python scripts/backup.py
```

Logs:

```bash
journalctl --user -u theoros-backup -f
journalctl --user -u theoros-backup --since today
```

Timer schedule check:

```bash
systemctl --user list-timers theoros-backup.timer
```

## Restore

Fetch a bundle from either side. From the HDD:

```bash
ls /mnt/backup/theoros/
cp /mnt/backup/theoros/theoros-backup-YYYY-MM-DD.tar.gz.age /tmp/
```

From cloud:

```bash
rclone lsf gdrive:theoros-backups/
rclone copy gdrive:theoros-backups/theoros-backup-YYYY-MM-DD.tar.gz.age /tmp/
```

Decrypt and extract into a scratch directory (never over the live tree
without a diff — a restore is a decision, not a script):

```bash
mkdir -p /tmp/theoros-restore
age -d -i ~/.config/theoros/backup-age.key \
    /tmp/theoros-backup-YYYY-MM-DD.tar.gz.age \
    | tar xz -C /tmp/theoros-restore
```

The archive contains `theoros.db` at the top level and a `reflections/`
subtree. Inspect, then decide whether to swap files into
`memory/system/` and `memory/reflections/` — with the capture worker
stopped first, so the DB swap doesn't race a live writer:

```bash
systemctl --user stop theoros-capture theoros-distillation
# ...swap files...
systemctl --user start theoros-capture theoros-distillation
```

## Exit codes

- `0` — cloud copy succeeded (HDD copy may have succeeded, failed, or
  been skipped because the disk wasn't mounted).
- `1` — cloud copy or prune failed. `systemctl status` will show this
  as failed; investigate via `journalctl`.
- `2` — configuration error (missing source DB or missing recipient
  file). Fix and rerun manually.

The asymmetry is deliberate: HDD absence is a normal condition (the
disk isn't always powered on), so it's a warning, not a failure. Cloud
absence isn't normal — if Google Drive is unreachable for a night we
want to know.
