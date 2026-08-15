"""Entrypoint for `python -m theoros.services.capture`.

Starts the capture service using uvicorn with settings from the
environment. Host is locked to localhost by default — the service
should never be reachable from the network.
"""

from __future__ import annotations

import uvicorn

from .config import CaptureSettings


def main() -> None:
    """Run the capture service."""
    settings = CaptureSettings()
    uvicorn.run(
        "theoros.services.capture.app:app",
        host=settings.theoros_capture_host,
        port=settings.theoros_capture_port,
        # Import string instead of the app object directly so uvicorn
        # can manage the event loop properly and reload works if we
        # ever want it during development.
    )


if __name__ == "__main__":
    main()
