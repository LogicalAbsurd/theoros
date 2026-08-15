# Theoros Window Meta — KWin script

KWin script that tracks window focus changes on Plasma 6 / Wayland and
reports them to the Theoros capture daemon via D-Bus.

## How it works

The script runs inside the KWin compositor.  On every focus change it
calls `workspace.windowActivated`, extracts five fields from the
focused window (`caption`, `resourceClass`, `resourceName`, `pid`,
`internalId`), and sends them to the Python daemon's D-Bus method
`NotifyFocusChanged` via KWin's `callDBus()`.

KWin's JavaScript scripting engine can **call** D-Bus methods but
cannot **register** its own.  So the daemon (`org.theoros.WindowMeta`)
is the component that owns the bus name and exposes:

- **Signal** `WindowFocusChanged` — re-emitted whenever this script
  reports a focus change.
- **Method** `GetActiveWindow` — returns the last window state this
  script reported.

The script also reports the currently-focused window on load, so if
the daemon is already running it gets initial state without waiting for
a focus change.

## Directory layout

```
kwin_script/
  metadata.json              # KPlugin metadata (Plasma 6 format)
  contents/
    code/
      main.js                # the script
  README.md                  # this file
```

## Install

Symlink (or copy) this directory into the KWin scripts location under
the plugin ID `theoros-window-meta`:

```bash
ln -s "$(pwd)" ~/.local/share/kwin/scripts/theoros-window-meta
```

(Run from inside `src/theoros/clients/kwin_script/`.)

## Load and enable

### Option A: persistent (survives logout)

```bash
# Enable the plugin in kwinrc
kwriteconfig6 --file kwinrc --group Plugins \
  --key theoros-window-metaEnabled true

# Tell KWin to pick up the change
qdbus org.kde.KWin /KWin reconfigure
```

To disable later:

```bash
kwriteconfig6 --file kwinrc --group Plugins \
  --key theoros-window-metaEnabled false
qdbus org.kde.KWin /KWin reconfigure
```

### Option B: one-shot (for testing, does not survive logout)

```bash
# Load the script — returns a script number N
SCRIPT_NUM=$(qdbus org.kde.KWin /Scripting loadScript \
  "$HOME/.local/share/kwin/scripts/theoros-window-meta/contents/code/main.js" \
  theoros-window-meta)

# Run it
qdbus org.kde.KWin /Scripting/Script$SCRIPT_NUM run
```

## Verify

With the daemon **not** running, you can still confirm the script
loaded by watching KWin's journal for callDBus failures (expected when
the daemon bus name is unowned):

```bash
journalctl --user -u plasma-kwin_wayland -f
```

With the daemon running, use `gdbus monitor` to watch the daemon's
signal emissions:

```bash
gdbus monitor --session --dest org.theoros.WindowMeta \
  --object-path /org/theoros/WindowMeta
```

Then switch windows — you should see `WindowFocusChanged` signals.

## Uninstall

```bash
# Disable
kwriteconfig6 --file kwinrc --group Plugins \
  --key theoros-window-metaEnabled false
qdbus org.kde.KWin /KWin reconfigure

# Remove symlink
rm ~/.local/share/kwin/scripts/theoros-window-meta
```

## Dependencies

None beyond KDE Plasma 6 and KWin (already running on the operator's
machine).  The script uses only the built-in KWin scripting API:
`workspace.windowActivated`, `workspace.activeWindow`, `callDBus()`.
