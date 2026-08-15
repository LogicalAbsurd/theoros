# Theoros capture service — systemd setup

## Prerequisites

Run database migrations before starting the service. The capture
service writes to `raw_events` and will fail immediately if the table
doesn't exist.

```bash
.venv/bin/python -m theoros.db.migrate
```

## Install

Unit files in this directory use `/opt/theoros` as the working
directory. If you check out the repo elsewhere, edit the paths in each
unit or symlink the checkout to `/opt/theoros`.

Symlink the unit file into the user systemd directory, reload, and
enable:

```bash
mkdir -p ~/.config/systemd/user
ln -s /opt/theoros/deploy/systemd/theoros-capture.service \
      ~/.config/systemd/user/theoros-capture.service
systemctl --user daemon-reload
systemctl --user enable --now theoros-capture
```

`enable --now` both enables the service (so it starts on login) and
starts it immediately.

## Status

```bash
systemctl --user status theoros-capture
```

## Logs

Follow logs in real time:

```bash
journalctl --user -u theoros-capture -f
```

Logs since today:

```bash
journalctl --user -u theoros-capture --since today
```

## Stop / start / restart

```bash
systemctl --user stop theoros-capture
systemctl --user start theoros-capture
systemctl --user restart theoros-capture
```

## Uninstall

```bash
systemctl --user disable --now theoros-capture
rm ~/.config/systemd/user/theoros-capture.service
systemctl --user daemon-reload
```

## Other services

The same install / status / logs / uninstall commands apply to the
other units in this directory — swap `theoros-capture` for the unit
name. Notable ones:

- `theoros-reader.service` (Phase 5): serves the phone-friendly
  reflections view on `127.0.0.1:8766`. Reached from the phone via
  the cloudflared tunnel on its own hostname, guarded by a
  Cloudflare Access user-login policy scoped to your email. Requires
  `THEOROS_READER_CF_AUD` and `THEOROS_READER_ALLOWED_EMAIL` in
  `.env` before the auth check does anything (empty = allow all,
  local-dev posture). Also needs a new cloudflared ingress entry and
  DNS record for the reader hostname (out-of-repo).
- `theoros-window-meta.service` / `theoros-ocr.service`: both carry a
  `DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus` override so they can
  find the session bus at boot before the graphical session exports
  its env. If you had a local drop-in adding the same, it can be
  removed after `daemon-reload` picks up the in-repo units.
