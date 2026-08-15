"""Pydantic models for the capture service's request and response bodies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CaptureEvent(BaseModel):
    """Incoming capture event from a browser extension, AT-SPI daemon, or OCR tier.

    Clients send what they know; the service computes content_length,
    content_hash, and received_at itself.
    """

    captured_at: datetime = Field(
        description="ISO-8601 UTC timestamp of when the capture occurred on the operator's machine.",
    )
    source_tier: Literal["browser", "atspi", "ocr", "window_meta", "mobile"] = Field(
        description="Which capture tier produced this event.",
    )
    source_app: str | None = Field(
        default=None,
        description="Application name (e.g. 'firefox', 'obsidian'). Nullable for OCR.",
    )
    source_identifier: str | None = Field(
        default=None,
        description="Tier-specific location: URL for browser, window title or AT-SPI path for atspi/ocr.",
    )
    title: str | None = Field(
        default=None,
        description="Human-readable label — page title, window title, document name.",
    )
    content_text: str | None = Field(
        default=None,
        description="The captured text. Can be None for metadata-only OCR events.",
    )
    confidence: float | None = Field(
        default=None,
        description="0.0–1.0 confidence score. Meaningful mainly for OCR.",
    )
    raw_metadata: dict[str, Any] | None = Field(
        default=None,
        description="Tier-specific extras as a JSON-serializable dict.",
    )


class CaptureResponse(BaseModel):
    """Returned after a successful capture insert."""

    id: int
    received_at: str = Field(
        description="ISO-8601 UTC timestamp of when the row was written.",
    )
    duplicate: bool = Field(
        default=False,
        description="True if this capture was deduplicated against an existing row.",
    )


class HealthResponse(BaseModel):
    """Response from GET /health."""

    status: Literal["ok", "degraded"]
    error: str | None = None


class StatusResponse(BaseModel):
    """Response from GET /status."""

    total_events: int
    last_capture_at: str | None = Field(
        description="ISO-8601 UTC timestamp of the most recent captured_at, or null if no events.",
    )
    database_path: str

class UrlExclusionCreate(BaseModel):
    """Request body for POST /exclusions/urls."""

    kind: Literal["domain", "path_prefix"] = Field(
        description="Match type. 'domain' matches the full host; 'path_prefix' matches a URL path prefix.",
    )
    value: str = Field(
        min_length=1,
        description="The domain (e.g. 'example.com') or path prefix (e.g. 'github.com/sensitive-org/') to exclude.",
    )


class UrlExclusionDelete(BaseModel):
    """Request body for DELETE /exclusions/urls.

    Body-bearing DELETE because the alternative — path params with the
    domain/prefix URL-encoded into the path — gets ugly for values
    containing dots and slashes.
    """

    kind: Literal["domain", "path_prefix"]
    value: str = Field(min_length=1)


class UrlExclusionsList(BaseModel):
    """Response from GET /exclusions/urls.

    Two flat lists rather than a list of {kind, value} objects so the
    browser extension can drop the response straight into its existing
    in-memory model: { domains: [...], path_prefixes: [...] }.
    """

    domains: list[str] = Field(default_factory=list)
    path_prefixes: list[str] = Field(default_factory=list)


class UrlExclusionMutationResponse(BaseModel):
    """Response from POST and DELETE /exclusions/urls.

    Indicates whether the operation changed state. For POST: True if a
    new row was inserted, False if the row already existed (idempotent).
    For DELETE: True if a row was removed, False if it wasn't there.
    """

    changed: bool
