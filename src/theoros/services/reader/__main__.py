"""Entrypoint for `python -m theoros.services.reader`."""

from __future__ import annotations

import uvicorn

from .config import ReaderSettings


def main() -> None:
    settings = ReaderSettings()
    uvicorn.run(
        "theoros.services.reader.app:app",
        host=settings.theoros_reader_host,
        port=settings.theoros_reader_port,
    )


if __name__ == "__main__":
    main()
