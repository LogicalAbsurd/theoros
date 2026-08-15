# Phase 2b: KWin-based window metadata capture (KDE Plasma)

## Context

Theoros captures browser content via the Firefox extension. Phase 2b
expands capture to native applications. On the operator's machine
(Fedora 43, KDE Plasma on Wayland), the capture surface is window
metadata: focused app, window title, focus duration. Content
extraction from native apps is deferred to Phase 2b-content.

## Why KDE-native, not a generic solution

Earlier discussion considered GNOME-Shell-extension-based approaches
(window-calls-extended, focused-window-dbus). All GNOME-only — KDE
Plasma uses KWin, not GNOME Shell, and these extensions don't apply.

KDE has native Wayland window-info access via KWin's D-Bus interface
and KWin Scripting. Verified on the operator's system:
- `qdbus org.kde.KWin /KWin` exposes the methods
- `qdbus org.kde.KWin /Scripting` exposes script loading
- `activeOutputName` returns non-interactively (no crosshair) —
  proves KWin answers "active" queries without user input on Wayland

## Architecture: two pieces

### Piece 1: KWin script (JavaScript, runs inside KWin compositor)

A small script (~50 lines) loaded into KWin via the Scripting D-Bus
interface. The script:

1. Subscribes to KWin's internal `windowActivated` signal.
2. On each focus change, extracts: window title, wm_class, pid,
   process name, timestamp.
3. Exposes a custom D-Bus interface emitting a signal
   `WindowFocusChanged` with that payload.
4. Also exposes a `GetActiveWindow` method so the daemon can query
   on startup (catches "what's focused right now" without waiting
   for a focus change).

This runs inside KWin — same execution context as KDE's effects and
tiling tools. First-class KDE, no third-party dependencies.

### Piece 2: Python daemon (theoros-window-meta)

A new persistent process under systemd user supervision. Mirrors the
shape of the existing capture service.

1. Connects to the KWin script's custom D-Bus interface.
2. On startup: calls `GetActiveWindow` to seed initial state.
3. Listens for `WindowFocusChanged` signals.
4. For each focus change: POSTs a capture event to the existing
   capture service (http://127.0.0.1:8765/capture) with:
   - source_tier = "window_meta" (new tier — see below)
   - source_app = wm_class or process name
   - source_identifier = a stable identifier (process name + window
     id, or similar)
   - title = window title
   - content_text = NULL (no content, this is metadata only)
   - raw_metadata = { pid, wm_class, kwin_window_id }

Filtering: skip Firefox windows. The browser extension already
captures from Firefox; window-meta capture would duplicate. Possibly
also skip terminal windows running theoros itself, to avoid feedback.
Define exclusions in the daemon's config.

## Schema change: new source_tier

The current CHECK constraint is `source_tier IN ('browser', 'atspi',
'ocr')`. window_meta isn't any of those.

Migration 006 will widen the CHECK constraint to include
`'window_meta'`. Standard SQLite recreate pattern (same as 002, 004).

Existing rows are unaffected — they're all 'browser' tier today.

## How chain logic interacts

Window-meta events have no content_text. The chain logic in
insert_raw_event will short-circuit on the existing `if redacted_text
is not None and tab_session_id and event.source_identifier` guard —
window-meta rows are single-shot captures with chain_position 1,
is_chain_root 1, no continuation.

This is correct: a focus event is a point-in-time fact. No "delta"
applies.

## What this gives Theoros

For the operator's specified use cases:
- LibreOffice — captures focus events with window title showing the
  document name. Content extraction deferred.
- Notesnook — same.
- Thunderbird — same.
- Discord (browser) — already covered by browser extension.

Even without content, window-meta captures give Theoros the *shape*
of the operator's day across native apps: which apps, when, for how
long, in what order. That's real signal for Phase 4 reflections
("you spent 40 minutes in Notesnook this morning, then 2 hours in
LibreOffice, then back to Notesnook").

## Phased approach

### Phase 2b-meta (this work)

1. KWin script: write, install to ~/.local/share/kwin/scripts/, load
   via D-Bus, verify it's emitting signals.
2. Migration 006: widen source_tier CHECK to include 'window_meta'.
3. Python daemon: implement, test against capture service.
4. systemd user unit: theoros-window-meta.service. Same shape as
   theoros-capture.service.

### Phase 2b-content (later)

Per-app text extraction. Different approach per app:
- KDE/Qt apps: AT-SPI works (KDE supports AT-SPI as accessibility
  framework).
- LibreOffice: AT-SPI works well.
- Electron apps (Notesnook, Discord desktop): AT-SPI patchy but
  possible.
- Thunderbird: AT-SPI works.

Defer until Phase 2b-meta is stable and producing useful signal.

## Where to start next session

1. Run `qdbus org.kde.KWin /KWin org.kde.KWin.queryWindowInfo` to
   confirm queryWindowInfo is interactive (cursor crosshair). This
   confirms we need the script-based approach and can't just poll.
2. Have Claude Code write the KWin script as
   src/theoros/clients/kwin_script/. Include README with install
   instructions.
3. Test the script in isolation with gdbus before building the
   daemon.
4. Then daemon, migration, systemd unit.

## Operator test commands (reference)

After installing the script:

```bash
# Verify the script is loaded
qdbus org.kde.kwin.Scripting isScriptLoaded theoros-window-meta

# Listen to signals (Ctrl+C to stop)
gdbus monitor --session --dest org.kde.kwin --object-path \
  /org/kde/kwin/theoros_window_meta

# Query active window
qdbus org.kde.kwin /org/kde/kwin/theoros_window_meta GetActiveWindow
```

(Exact paths/names will be set by the script implementation.)
