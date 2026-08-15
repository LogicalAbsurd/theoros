"""Reader service configuration, loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parents[4]


class ReaderSettings(BaseSettings):
    """Settings for the reader service, populated from env vars."""

    # Bind address.  Same reasoning as capture: localhost-only, exposed
    # to the internet through the cloudflared tunnel and never directly.
    theoros_reader_host: str = "127.0.0.1"
    theoros_reader_port: int = 8766

    # Cloudflare Access application AUD for the reader.  Different from
    # the capture AUD because the reader lives on a different hostname
    # (e.g. reflections.example.com) with its own Access app and its
    # own user-login policy.  Pulled from the Cf-Access-Aud header after
    # the Access app is created — leave empty to disable auth locally.
    theoros_reader_cf_aud: str = ""

    # Cloudflare Zero Trust team domain (e.g. "yourteam.cloudflareaccess.com").
    # Same team as capture; separate field only so this service module
    # can be configured without importing capture settings.  Leave empty
    # for local development.
    theoros_reader_cf_team_domain: str = ""

    # Email that the reader accepts.  User-login JWTs from Cloudflare
    # Access carry the authenticated identity in the `email` claim; we
    # allow only this address through even though the Access policy
    # should already be scoped to it (defense-in-depth against a
    # misconfigured policy).
    theoros_reader_allowed_email: str = ""

    # Localhost bypass: requests originating on 127.0.0.1 don't traverse
    # Cloudflare and won't carry a JWT.  Lets the operator hit the
    # reader from the desktop browser without going through the tunnel.
    theoros_reader_allow_localhost_bypass: bool = True

    # Page size for the index.  Reflections accumulate slowly (one daily
    # + one weekly per week), so a single page comfortably holds many
    # months; pagination is a nicety, not a necessity.
    theoros_reader_page_size: int = 60

    model_config = {
        "env_file": str(REPO_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }
