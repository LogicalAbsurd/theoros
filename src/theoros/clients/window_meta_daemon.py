"""Theoros window-meta daemon — Phase 2b-meta.

D-Bus service that receives window focus-change notifications from the
KWin script (via NotifyFocusChanged), stores the last known window
state, re-emits a WindowFocusChanged D-Bus signal for external
listeners, and POSTs each non-excluded event to the capture service as
a source_tier='window_meta' record.

Bus name:   org.theoros.WindowMeta
Object:     /org/theoros/WindowMeta
Interface:  org.theoros.WindowMeta

Run:
    python -m theoros.clients.window_meta_daemon

Dependencies beyond requirements.txt:
    PyGObject (gi)  — GLib main loop; install as system package
                      (python3-gobject on Fedora).  Create the venv
                      with --system-site-packages or install pygobject
                      via pip (needs system headers).
"""

from __future__ import annotations

import json
import logging
import signal
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pydantic_settings import BaseSettings

from dasbus.connection import SessionMessageBus
from dasbus.signal import Signal
from gi.repository import GLib


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# window_meta_daemon.py lives at src/theoros/clients/ — three parents
# up from this file to the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]


class WindowMetaSettings(BaseSettings):
    """Settings loaded from environment / .env file.

    Complex types (list, dict) accept JSON-encoded env-var values.
    For example::

        THEOROS_WINDOW_META_EXCLUDED_CLASSES='["firefox","Navigator"]'
    """

    # Capture service endpoint.  Must include the path (/capture).
    theoros_capture_url: str = "http://127.0.0.1:8765/capture"

    # HTTP timeout for the POST.  Short because the capture service is
    # on localhost; a longer timeout would block the GLib main loop and
    # delay processing of subsequent D-Bus calls.
    theoros_capture_timeout: int = 2

    # resourceClass values to skip unconditionally (case-insensitive
    # substring match).  Firefox and derivatives are excluded because
    # the browser extension already captures their content — window-meta
    # would duplicate the events without adding information.
    theoros_window_meta_excluded_classes: list[str] = [
        "firefox",
        "Navigator",
        "firefox-esr",
        "librewolf",
    ]

    # Conditional exclusions: {resourceClass_substring: caption_keyword}.
    # An event is skipped only when BOTH the class and caption match
    # (case-insensitive).  Default: Konsole windows whose title contains
    # "theoros" — avoids the daemon's own terminal creating a feedback
    # loop in the capture log.
    theoros_window_meta_caption_exclusions: dict[str, str] = {
        "konsole": "theoros",
        "org.kde.konsole": "theoros",
    }

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


log = logging.getLogger("theoros.window_meta")


# ---------------------------------------------------------------------------
# Exclusion logic
# ---------------------------------------------------------------------------

def _is_excluded(
    resource_class: str,
    caption: str,
    settings: WindowMetaSettings,
) -> bool:
    """Return True if this focus event should not be posted to capture."""
    rc_lower = resource_class.lower()

    for pattern in settings.theoros_window_meta_excluded_classes:
        if pattern.lower() in rc_lower:
            return True

    for cls_pattern, kw in settings.theoros_window_meta_caption_exclusions.items():
        if cls_pattern.lower() in rc_lower and kw.lower() in caption.lower():
            return True

    return False


# ---------------------------------------------------------------------------
# Capture-service POST
# ---------------------------------------------------------------------------

def _post_capture_event(
    caption: str,
    resource_class: str,
    resource_name: str,
    pid: int,
    uuid: str,
    settings: WindowMetaSettings,
) -> None:
    """POST a window_meta event to the local capture service.

    Uses urllib.request (stdlib) to avoid adding a dependency.  The call
    is synchronous and blocks the GLib main loop — acceptable because
    the service is on localhost (~1 ms) and focus changes are infrequent.
    If the capture service is down or rejects the payload (e.g. 422
    because migration 006 hasn't run yet), we log and move on.
    """
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_tier": "window_meta",
        "source_app": resource_class or resource_name,
        "source_identifier": f"{resource_class}:{uuid}" if uuid else resource_class,
        "title": caption,
        "content_text": None,
        "raw_metadata": {
            "pid": pid,
            "wm_class": resource_class,
            "resource_name": resource_name,
            "kwin_window_id": uuid,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        settings.theoros_capture_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.theoros_capture_timeout) as resp:
            log.info("POST %s → %d", caption[:60], resp.status)
    except Exception as exc:
        log.warning("POST failed (%s): %s", caption[:60], exc)


# ---------------------------------------------------------------------------
# D-Bus service object
# ---------------------------------------------------------------------------

class WindowMetaService:
    """Published on the session bus at /org/theoros/WindowMeta.

    The KWin script calls NotifyFocusChanged via callDBus() on every
    window focus change.  This object stores the state, re-emits a
    D-Bus signal, and forwards non-excluded events to the capture
    service.
    """

    # GDBus introspection XML — keep in sync with the method and signal
    # signatures below.  Tools like gdbus and qdbus read this to
    # discover what the object exposes.
    __dbus_xml__ = """
    <node>
        <interface name="org.theoros.WindowMeta">
            <method name="NotifyFocusChanged">
                <arg direction="in" name="caption" type="s"/>
                <arg direction="in" name="resourceClass" type="s"/>
                <arg direction="in" name="resourceName" type="s"/>
                <arg direction="in" name="pid" type="i"/>
                <arg direction="in" name="uuid" type="s"/>
            </method>
            <method name="GetActiveWindow">
                <arg direction="out" name="caption" type="s"/>
                <arg direction="out" name="resourceClass" type="s"/>
                <arg direction="out" name="resourceName" type="s"/>
                <arg direction="out" name="pid" type="i"/>
                <arg direction="out" name="uuid" type="s"/>
            </method>
            <signal name="WindowFocusChanged">
                <arg name="caption" type="s"/>
                <arg name="resourceClass" type="s"/>
                <arg name="resourceName" type="s"/>
                <arg name="pid" type="i"/>
                <arg name="uuid" type="s"/>
            </signal>
        </interface>
    </node>
    """

    def __init__(self, settings: WindowMetaSettings):
        self._settings = settings
        self.WindowFocusChanged = Signal()
        self._caption = ""
        self._resource_class = ""
        self._resource_name = ""
        self._pid = 0
        self._uuid = ""

    def NotifyFocusChanged(
        self,
        caption: str,
        resourceClass: str,
        resourceName: str,
        pid: int,
        uuid: str,
    ) -> None:
        """Receive a focus-change event from the KWin script."""
        # Always store latest state — even for excluded windows — so
        # GetActiveWindow returns the truth about what is focused.
        self._caption = caption
        self._resource_class = resourceClass
        self._resource_name = resourceName
        self._pid = pid
        self._uuid = uuid

        log.info("focus → %s [%s] pid=%d", caption[:80], resourceClass, pid)

        # Re-emit as a proper D-Bus signal so any external listener
        # (gdbus monitor, another tool) can subscribe without knowing
        # the KWin script exists.
        self.WindowFocusChanged.emit(
            caption, resourceClass, resourceName, pid, uuid
        )

        if _is_excluded(resourceClass, caption, self._settings):
            log.debug("skipped (excluded): %s [%s]", caption[:60], resourceClass)
            return

        _post_capture_event(
            caption, resourceClass, resourceName, pid, uuid,
            self._settings,
        )

    def GetActiveWindow(self) -> tuple:
        """Return the last reported window state.

        Returns empty strings / zero pid if no focus event has been
        received yet (daemon started before the KWin script, or no
        window is focused).
        """
        return (
            self._caption,
            self._resource_class,
            self._resource_name,
            self._pid,
            self._uuid,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = WindowMetaSettings()
    log.info("starting theoros-window-meta daemon")
    log.info("capture URL: %s", settings.theoros_capture_url)
    log.info("excluded classes: %s", settings.theoros_window_meta_excluded_classes)

    bus = SessionMessageBus()
    service = WindowMetaService(settings)
    bus.publish_object("/org/theoros/WindowMeta", service)
    bus.register_service("org.theoros.WindowMeta")
    log.info("D-Bus: registered org.theoros.WindowMeta")

    loop = GLib.MainLoop()

    # GLib owns the main loop, so use its signal handling.  Python's
    # signal.signal() doesn't reliably interrupt GLib.MainLoop.run().
    def _quit(sig):
        log.info("received signal %d, shutting down", sig)
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _quit, sig)

    try:
        loop.run()
    finally:
        bus.disconnect()
        log.info("theoros-window-meta daemon stopped")


if __name__ == "__main__":
    main()
