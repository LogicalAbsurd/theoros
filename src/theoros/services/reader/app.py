"""FastAPI app for the Theoros reader service.

Two pages: an index of promoted reflections (newest first, mixed daily
+ weekly), and a detail page rendering one reflection's markdown.  Both
gated by the require_cf_access dependency.  Deliberately no JS, no
build step, no client-side Supabase access — the page renders on the
server and the phone gets HTML.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import markdown as md
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import require_cf_access
from .config import ReaderSettings
from .supabase import (
    Reflection,
    ReflectionSummary,
    get_reflection,
    list_reflections,
)

logger = logging.getLogger(__name__)

settings = ReaderSettings()

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_date(value) -> str:
    """Jinja filter: format a datetime/date as YYYY-MM-DD."""
    return value.strftime("%Y-%m-%d")


def _title(item: ReflectionSummary | Reflection) -> str:
    """Human title, matching the on-disk weekly-file convention
    ("Weekly reflection - 2026-06-28 to 2026-07-05") so the reader
    reads the same as the source files."""
    start = item.period_start.strftime("%Y-%m-%d")
    if item.timeframe == "daily":
        return f"Daily reflection · {start}"
    end = item.period_end.strftime("%Y-%m-%d")
    return f"{item.timeframe.capitalize()} reflection · {start} to {end}"


templates.env.filters["date"] = _fmt_date
templates.env.globals["title_for"] = _title


app = FastAPI(
    title="Theoros Reader",
    description="Phone-friendly view of promoted reflections.",
)

# Serve stylesheet + any future static assets from /static/*.  Kept in
# a subdirectory rather than inlined so the page can cache its CSS
# separately from its HTML — trivial win on repeat visits.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Unauthenticated health probe for cloudflared / systemd."""
    return JSONResponse({"status": "ok"})


@app.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_cf_access)],
)
async def index(request: Request) -> HTMLResponse:
    reflections = list_reflections(limit=settings.theoros_reader_page_size)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "reflections": reflections,
            "page_title": "Theoros — reflections",
        },
    )


@app.get(
    "/{timeframe}/{day}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_cf_access)],
)
async def reflection_detail(
    request: Request, timeframe: str, day: str
) -> HTMLResponse:
    # Restrict to the two cadences we actually promote today.  Monthly
    # is defined in the schema but not yet produced; when it is, adding
    # it here is a one-line change.
    if timeframe not in ("daily", "weekly"):
        raise HTTPException(status_code=404, detail="Unknown timeframe")

    try:
        day_parsed = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=404, detail="Bad date")

    reflection = get_reflection(timeframe, day_parsed)
    if reflection is None:
        raise HTTPException(status_code=404, detail="Reflection not found")

    # Convert markdown once, server-side.  extras: none — the promoted
    # bodies are plain markdown from Claude, no tables or code fences
    # in the reflection register.  If that changes, add extensions here.
    body_html = md.markdown(reflection.body_markdown, output_format="html5")

    return templates.TemplateResponse(
        request,
        "reflection.html",
        {
            "reflection": reflection,
            "body_html": body_html,
            "page_title": _title(reflection),
        },
    )
