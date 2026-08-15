"""Theoros OCR daemon — Phase 2c.

Periodic fullscreen screenshot capture with OCR text extraction.
Takes a screenshot via spectacle, extracts text via pytesseract,
deduplicates against recent captures, and POSTs non-duplicate text
to the capture service as source_tier='ocr'.

Idle detection: subscribes to the window-meta daemon's
WindowFocusChanged D-Bus signal to track the last focus change.
If no focus change has occurred within a configurable idle threshold,
the capture cycle is skipped — no point OCR-ing a screen nobody is
looking at.

Run:
    python -m theoros.clients.ocr_daemon

System dependencies (not in requirements.txt):
    tesseract    — OCR engine.  On Fedora:
                   sudo dnf install tesseract tesseract-langpack-eng
    spectacle    — KDE screenshot tool (ships with Plasma)
    PyGObject    — GLib main loop for D-Bus signal subscription;
                   python3-gobject system package on Fedora
"""

from __future__ import annotations

import hashlib
import json
import logging
import signal
import subprocess
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import pytesseract
from pydantic_settings import BaseSettings

from dasbus.connection import SessionMessageBus
from gi.repository import GLib


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# ocr_daemon.py lives at src/theoros/clients/ — three parents up to
# the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]


class OcrSettings(BaseSettings):
    """Settings loaded from environment / .env file."""

    # Capture service endpoint.
    theoros_capture_url: str = "http://127.0.0.1:8765/capture"

    # HTTP timeout for the POST to the capture service.
    theoros_capture_timeout: int = 2

    # Seconds between capture cycles.
    theoros_ocr_interval: int = 30

    # Number of recent content hashes to keep for local dedup.  The
    # capture service also deduplicates via content_hash, but checking
    # locally avoids writing a screenshot, running tesseract, and
    # making a network call when the screen hasn't changed.
    theoros_ocr_dedup_window: int = 10

    # Skip capture if no window focus change in this many seconds.
    # Prevents burning CPU on tesseract while the operator is away.
    theoros_ocr_idle_threshold: int = 120

    # Directory for screenshot storage.
    theoros_ocr_screenshot_dir: str = str(
        REPO_ROOT / "memory" / "ocr" / "screenshots"
    )

    # Days to retain screenshot PNGs before deletion.  Extracted text
    # lives permanently in the raw_events table; the images are kept
    # only for debugging and retroactive re-OCR with a better model.
    theoros_ocr_retention_days: int = 14

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


log = logging.getLogger("theoros.ocr")


# ---------------------------------------------------------------------------
# Idle detection via D-Bus
# ---------------------------------------------------------------------------

class ActivityTracker:
    """Tracks the last window focus change via the window-meta D-Bus signal.

    If the window-meta daemon is not running, the tracker defaults to
    "always active" — the OCR daemon degrades to a simple interval
    timer rather than refusing to capture.
    """

    def __init__(self) -> None:
        self._last_activity: float = monotonic()

    def on_focus_changed(self, *_args: object) -> None:
        """D-Bus signal callback.  Arguments are ignored; only the
        timestamp matters."""
        self._last_activity = monotonic()

    def seconds_since_activity(self) -> float:
        return monotonic() - self._last_activity

    def is_idle(self, threshold: int) -> bool:
        return self.seconds_since_activity() > threshold


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------

def _take_screenshot(output_path: Path) -> bool:
    """Take a fullscreen screenshot via spectacle.  Returns True on success."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "spectacle",
                "--background",
                "--nonotify",
                "--fullscreen",
                "--output",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return output_path.exists()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("spectacle failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# OCR extraction
# ---------------------------------------------------------------------------

def _extract_text(image_path: Path) -> tuple[str, float]:
    """Extract text from an image via pytesseract.

    Returns (text, confidence).  Confidence is the mean of
    pytesseract's per-word confidence scores (excluding non-text
    regions), normalized to 0.0–1.0.
    """
    try:
        # image_to_data returns per-word bounding boxes and confidence
        # scores.  We use it for the confidence value, then call
        # image_to_string for the clean text (image_to_data's text
        # column lacks the whitespace reconstruction that
        # image_to_string provides).
        data = pytesseract.image_to_data(
            str(image_path), output_type=pytesseract.Output.DICT
        )
        confidences = [c for c in data["conf"] if c > 0]
        mean_conf = (
            sum(confidences) / len(confidences) / 100.0
            if confidences
            else 0.0
        )

        text = pytesseract.image_to_string(str(image_path)).strip()
        return text, round(mean_conf, 3)
    except Exception as exc:
        log.warning("OCR extraction failed: %s", exc)
        return "", 0.0


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    """SHA-256 hex digest, matching the capture service's hash scheme."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Capture-service POST
# ---------------------------------------------------------------------------

def _post_capture(
    text: str,
    confidence: float,
    screenshot_name: str,
    settings: OcrSettings,
) -> None:
    """POST an OCR capture event to the local capture service."""
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_tier": "ocr",
        "source_app": "ocr",
        "source_identifier": screenshot_name,
        "title": None,
        "content_text": text,
        "confidence": confidence,
        "raw_metadata": {
            "screenshot_file": screenshot_name,
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
        with urllib.request.urlopen(
            req, timeout=settings.theoros_capture_timeout
        ) as resp:
            log.info(
                "POST → %d (%d chars, conf=%.2f)",
                resp.status,
                len(text),
                confidence,
            )
    except Exception as exc:
        log.warning("POST failed: %s", exc)


# ---------------------------------------------------------------------------
# Screenshot retention cleanup
# ---------------------------------------------------------------------------

def _cleanup_old_screenshots(screenshot_dir: Path, retention_days: int) -> None:
    """Delete screenshot PNGs older than retention_days."""
    if not screenshot_dir.exists():
        return

    now = datetime.now(timezone.utc)
    for path in screenshot_dir.iterdir():
        if not path.is_file() or path.suffix.lower() != ".png":
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age_days = (now - mtime).days
        if age_days >= retention_days:
            path.unlink()
            log.info("deleted old screenshot: %s (%d days old)", path.name, age_days)


# ---------------------------------------------------------------------------
# Main capture cycle
# ---------------------------------------------------------------------------

def _capture_cycle(
    settings: OcrSettings,
    tracker: ActivityTracker,
    recent_hashes: deque,
) -> bool:
    """Run one capture cycle.

    Returns True to keep the GLib timeout alive (GLib.SOURCE_CONTINUE).
    """
    # Retention runs first, before any early returns, so screenshots
    # keep aging out even during long idle stretches when no capture
    # completes.
    screenshot_dir = Path(settings.theoros_ocr_screenshot_dir)
    _cleanup_old_screenshots(screenshot_dir, settings.theoros_ocr_retention_days)

    # Skip if idle.
    if tracker.is_idle(settings.theoros_ocr_idle_threshold):
        log.debug(
            "idle (%.0fs since last focus change), skipping",
            tracker.seconds_since_activity(),
        )
        return True

    # Take screenshot.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    screenshot_path = screenshot_dir / f"{timestamp}.png"

    if not _take_screenshot(screenshot_path):
        return True

    # Extract text.
    text, confidence = _extract_text(screenshot_path)
    if not text:
        log.debug("no text extracted, skipping")
        return True

    # Dedup against recent hashes.
    h = _content_hash(text)
    if h in recent_hashes:
        log.debug("duplicate content, skipping")
        return True

    recent_hashes.append(h)

    # POST to capture service.
    _post_capture(text, confidence, screenshot_path.name, settings)

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = OcrSettings()
    log.info("starting theoros-ocr daemon")
    log.info("capture URL: %s", settings.theoros_capture_url)
    log.info(
        "interval: %ds, idle threshold: %ds",
        settings.theoros_ocr_interval,
        settings.theoros_ocr_idle_threshold,
    )
    log.info("screenshot dir: %s", settings.theoros_ocr_screenshot_dir)
    log.info(
        "retention: %d days, dedup window: %d hashes",
        settings.theoros_ocr_retention_days,
        settings.theoros_ocr_dedup_window,
    )

    tracker = ActivityTracker()
    recent_hashes: deque[str] = deque(maxlen=settings.theoros_ocr_dedup_window)

    # Subscribe to window-meta D-Bus signal for idle detection.
    # If the window-meta daemon isn't running, the tracker stays at
    # "last activity = now" and never triggers idle — the daemon
    # degrades to a simple interval timer.
    bus = SessionMessageBus()
    try:
        proxy = bus.get_proxy(
            "org.theoros.WindowMeta", "/org/theoros/WindowMeta"
        )
        proxy.WindowFocusChanged.connect(tracker.on_focus_changed)
        log.info("subscribed to org.theoros.WindowMeta.WindowFocusChanged")
    except Exception as exc:
        log.warning(
            "could not subscribe to window-meta signal (%s) — "
            "idle detection disabled, will capture every interval",
            exc,
        )

    # Schedule the first capture cycle after one interval.  GLib
    # re-schedules automatically as long as the callback returns True.
    GLib.timeout_add_seconds(
        settings.theoros_ocr_interval,
        _capture_cycle,
        settings,
        tracker,
        recent_hashes,
    )

    loop = GLib.MainLoop()

    # GLib owns the main loop, so use its signal handling.  Python's
    # signal.signal() doesn't reliably interrupt GLib.MainLoop.run().
    def _quit(sig: int) -> bool:
        log.info("received signal %d, shutting down", sig)
        loop.quit()
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _quit, sig)

    try:
        loop.run()
    finally:
        bus.disconnect()
        log.info("theoros-ocr daemon stopped")


if __name__ == "__main__":
    main()
