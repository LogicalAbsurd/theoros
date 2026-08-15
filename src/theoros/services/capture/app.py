"""FastAPI application for the Theoros capture service.

Receives capture events from browser extensions (and later AT-SPI and
OCR tiers) and writes them to the local SQLite raw_events table. This
is the write side of the capture pipeline; read paths and normalization
are separate services.

Run via `python -m theoros.services.capture` or with uvicorn directly:

    uvicorn theoros.services.capture.app:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import CaptureSettings
from .db import (
    check_db_health,
    check_raw_events_table,
    delete_url_exclusion,
    get_event_count,
    get_last_capture_at,
    insert_raw_event,
    insert_url_exclusion,
    list_url_exclusions,
)
from .models import (
    CaptureEvent,
    CaptureResponse,
    HealthResponse,
    StatusResponse,
    UrlExclusionCreate,
    UrlExclusionDelete,
    UrlExclusionMutationResponse,
    UrlExclusionsList,
)
from .auth import require_cf_access

logger = logging.getLogger(__name__)

settings = CaptureSettings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle for the capture service.

    On startup, verify the database exists and the raw_events table is
    present. This fails loudly rather than waiting for the first request
    to discover a missing schema — better to surface the problem
    immediately with a clear error message.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = settings.db_path
    logger.info("capture service starting, database: %s", db_path)

    # Fail fast if migrations haven't been run.
    check_raw_events_table(db_path)

    logger.info("raw_events table verified, service ready")
    yield
    logger.info("capture service shutting down")


app = FastAPI(
    title="Theoros Capture Service",
    description="Receives capture events and writes them to the local SQLite database.",
    lifespan=lifespan,
)

# Browser extensions run in moz-extension:// or chrome-extension:// origins,
# which the browser treats as cross-origin relative to http://127.0.0.1.
# Without CORS headers the browser blocks the POST from the extension.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(moz-extension|chrome-extension)://.*$|^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$",
    allow_methods=["POST", "GET", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)


@app.post("/capture", dependencies=[Depends(require_cf_access)])
async def capture(event: CaptureEvent) -> JSONResponse:
    """Accept a capture event and write it to raw_events.

    Returns 201 for new inserts, 200 for deduplicated captures (same
    tab_session_id + content_hash already stored).
    """
    db_path = settings.db_path
    row_id, received_at, was_duplicate, content_hash, chain_id, chain_position, lcp_length = (
        insert_raw_event(db_path, event)
    )

    if was_duplicate:
        tab_session_id = (event.raw_metadata or {}).get("tab_session_id", "?")
        logger.info(
            "deduped: tab_session=%s content_hash=%s existing_id=%d",
            tab_session_id,
            content_hash[:12],
            row_id,
        )
        return JSONResponse(
            status_code=200,
            content=CaptureResponse(
                id=row_id, received_at=received_at, duplicate=True
            ).model_dump(),
        )

    # Chain-aware "captured:" logging is handled by insert_raw_event in db.py,
    # which has the full chain context (LCP length, delta length).

    return JSONResponse(
        status_code=201,
        content=CaptureResponse(
            id=row_id, received_at=received_at, duplicate=False
        ).model_dump(),
    )


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse | JSONResponse:
    """Report whether the service is alive and can reach the database."""
    error = check_db_health(settings.db_path)
    if error is None:
        return HealthResponse(status="ok")
    logger.error("health check failed: %s", error)
    # Return 503 so load balancers (or a future systemd health check)
    # can detect a degraded service.
    return JSONResponse(
        status_code=503,
        content=HealthResponse(status="degraded", error=error).model_dump(),
    )


@app.get("/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    """Return basic statistics about the raw_events table."""
    db_path = settings.db_path
    return StatusResponse(
        total_events=get_event_count(db_path),
        last_capture_at=get_last_capture_at(db_path),
        database_path=str(db_path),
    )


# --- URL exclusion endpoints ---
# Same auth posture as /capture: Depends(require_cf_access) gates mobile
# access through the Cloudflare tunnel while the localhost bypass lets
# the browser extension through without a token.


@app.get(
    "/exclusions/urls",
    response_model=UrlExclusionsList,
    dependencies=[Depends(require_cf_access)],
)
async def get_url_exclusions() -> UrlExclusionsList:
    """Return all URL exclusions split into domains and path_prefixes."""
    domains, path_prefixes = list_url_exclusions(settings.db_path)
    return UrlExclusionsList(domains=domains, path_prefixes=path_prefixes)


@app.post(
    "/exclusions/urls",
    response_model=UrlExclusionMutationResponse,
    dependencies=[Depends(require_cf_access)],
)
async def create_url_exclusion(body: UrlExclusionCreate) -> JSONResponse:
    """Add a URL exclusion. 201 if new, 200 if already present (idempotent)."""
    changed = insert_url_exclusion(settings.db_path, body.kind, body.value)
    return JSONResponse(
        status_code=201 if changed else 200,
        content=UrlExclusionMutationResponse(changed=changed).model_dump(),
    )


@app.delete(
    "/exclusions/urls",
    response_model=UrlExclusionMutationResponse,
    dependencies=[Depends(require_cf_access)],
)
async def remove_url_exclusion(body: UrlExclusionDelete) -> UrlExclusionMutationResponse:
    """Remove a URL exclusion. Always 200 (idempotent delete)."""
    changed = delete_url_exclusion(settings.db_path, body.kind, body.value)
    return UrlExclusionMutationResponse(changed=changed)
