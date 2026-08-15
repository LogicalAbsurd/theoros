"""Capture service configuration, loaded from environment variables.

Uses pydantic-settings so env vars map to typed fields automatically.
Relative database paths resolve against the repo root, matching the
convention in theoros.db.migrate.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

# config.py lives at src/theoros/services/capture/config.py — four
# parents up from this file to reach the repo root. Same approach as
# migrate.py (three parents from src/theoros/db/migrate.py).
REPO_ROOT = Path(__file__).resolve().parents[4]


class CaptureSettings(BaseSettings):
    """Settings for the capture service, populated from env vars."""

    # Path to the local SQLite database. If relative, resolved against the
    # repo root so the service works regardless of CWD.
    theoros_db_path: str = "memory/system/theoros.db"

    # Bind address. Locked to localhost — Theoros is a single-user local
    # service and must never listen on a public interface.
    theoros_capture_host: str = "127.0.0.1"

    # Listening port. 8765 is arbitrary but unlikely to collide with common
    # dev servers (8000, 8080, 3000) or the local LLM server (8000).
    theoros_capture_port: int = 8765

    # Cloudflare Access service token Client-ID expected on inbound
    # requests. When set, the auth dependency requires the
    # CF-Access-Client-Id header to match this value. When empty
    # (default), auth is disabled — useful for local-only development.
    theoros_cf_access_client_id: str = ""

    # Cloudflare Zero Trust team domain (e.g. "yourteam.cloudflareaccess.com").
    # Used to fetch the JWKS for JWT verification. Left empty for local
    # development; must be set when auth is active.
    theoros_cf_team_domain: str = ""

    # Cloudflare Access application AUD for the capture app. Pulled from
    # the Cf-Access-Aud header after the Access application is created.
    # Left empty for local development; must be set when auth is active.
    theoros_cf_aud: str = ""

    # When True, allow requests from 127.0.0.1 / localhost to bypass
    # the CF header check. Lets local capture clients (the browser
    # extension, native daemons) keep working without carrying a token.
    # Mobile traffic comes from Cloudflare's edge with a public source
    # IP, so it still goes through the header check.
    theoros_cf_allow_localhost_bypass: bool = True

    # Composition-collapse (atspi + mobile tiers only).  Rapid same-app
    # captures that each extend the previous — a text field being typed
    # into, a note being composed — are chained rather than stored as
    # independent rows, mirroring the browser tier's LCP chain but keyed
    # on (source_app, source_identifier) and gated by a freshness window
    # instead of a tab_session_id.  Chain reconstruction yields the
    # single final-form record; the intermediate deltas remain on disk
    # per the raw-events-never-discarded rule in docs/spec.md.
    #
    # freshness_seconds: max age of the prior capture for chain
    #   continuation.  Older than this and we start a new chain — the
    #   composition burst is considered over.
    # lcp_threshold: fraction of the prior text's length that the new
    #   capture's longest-common-prefix must reach for the new capture
    #   to count as an extension rather than a fresh composition.  0.85
    #   tolerates minor mid-string edits while still breaking the chain
    #   when the user clears a field and starts over.
    theoros_composition_freshness_seconds: int = 30
    theoros_composition_lcp_threshold: float = 0.85

    model_config = {
        # pydantic-settings loads from a .env file automatically when
        # env_file is set. python-dotenv must be installed for this to
        # work; it's listed in requirements.txt.
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        # Case-insensitive env-var matching so THEOROS_DB_PATH and
        # theoros_db_path both work.
        "case_sensitive": False,
        "extra": "ignore",
    }

    @property
    def db_path(self) -> Path:
        """Return the database path, resolved against the repo root if relative."""
        p = Path(self.theoros_db_path)
        if p.is_absolute():
            return p
        return REPO_ROOT / p
