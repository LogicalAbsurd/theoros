/*
 * Theoros Window Meta — KWin script (Plasma 6)
 *
 * Runs inside the KWin compositor. Subscribes to workspace.windowActivated
 * and reports each focus change to the theoros-window-meta daemon via D-Bus.
 *
 * Communication flow:
 *   KWin script  --callDBus-->  Python daemon (org.theoros.WindowMeta)
 *
 * The daemon owns the bus name and re-emits WindowFocusChanged as a proper
 * D-Bus signal for any other listeners.  It also exposes GetActiveWindow
 * using the last state this script reported.  KWin's JS scripting engine
 * can call D-Bus methods but cannot register its own — so the daemon is
 * the external interface and this script is the event source.
 */

var DBUS_SERVICE = "org.theoros.WindowMeta";
var DBUS_PATH    = "/org/theoros/WindowMeta";
var DBUS_IFACE   = "org.theoros.WindowMeta";

/**
 * Extract the fields we care about from a KWin Window object.
 * Returns null if the window is falsy (e.g. desktop got focus).
 */
function windowPayload(win) {
    if (!win) {
        return null;
    }
    return {
        caption:       win.caption       || "",
        resourceClass: win.resourceClass || "",
        resourceName:  win.resourceName  || "",
        pid:           win.pid           || 0,
        uuid:          String(win.internalId || "")
    };
}

/**
 * Send a focus-change notification to the daemon.
 * If the daemon isn't running yet the call silently fails — that's fine;
 * the daemon will get the current window when the script is re-run or
 * the next focus change occurs after the daemon starts.
 */
function reportWindow(win) {
    var info = windowPayload(win);
    if (!info) {
        return;
    }
    callDBus(
        DBUS_SERVICE, DBUS_PATH, DBUS_IFACE,
        "NotifyFocusChanged",
        info.caption,
        info.resourceClass,
        info.resourceName,
        info.pid,
        info.uuid
    );
}

// --- wiring ---

// Every focus change
workspace.windowActivated.connect(reportWindow);

// Current window on script load (so the daemon gets state immediately
// if it's already running, without waiting for a focus change)
reportWindow(workspace.activeWindow);
