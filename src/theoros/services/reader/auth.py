"""Cloudflare Access JWT validation for the reader service.

Mirrors the capture service's auth but expects a user-login JWT rather
than a service-token JWT.  User-login JWTs carry the authenticated
identity in the `email` claim; service-token JWTs carry it in
`common_name` and no `email` at all.  We reject anything that doesn't
match THEOROS_READER_ALLOWED_EMAIL — belt-and-suspenders over the
Access policy itself, which should already be scoped to that email.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from fastapi import Header, HTTPException, Request
from jwt import PyJWKClient

from .config import ReaderSettings

logger = logging.getLogger(__name__)

settings = ReaderSettings()

LOCALHOST_IPS = {"127.0.0.1", "::1"}

# Same team domain as capture — Cloudflare Access apps share the
# tenant-level JWKS regardless of which app issued the JWT.  Read from
# the environment so this file carries no account-specific identifiers.
CF_TEAM_DOMAIN = settings.theoros_reader_cf_team_domain

# Lazily constructed on first use so a missing team domain in local
# dev doesn't blow up at import time.
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if not CF_TEAM_DOMAIN:
            raise RuntimeError(
                "THEOROS_READER_CF_TEAM_DOMAIN is not set but Cloudflare Access auth is active."
            )
        _jwks_client = PyJWKClient(
            f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs"
        )
    return _jwks_client


async def require_cf_access(
    request: Request,
    cf_jwt: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> None:
    """FastAPI dependency: validate Cloudflare Access user-login JWT.

    Returns None on success, raises HTTPException(403) on failure.
    """
    expected_aud = settings.theoros_reader_cf_aud
    expected_email = settings.theoros_reader_allowed_email

    # Auth disabled: no AUD configured means local development / initial
    # setup.  Same posture as capture — makes it trivial to run the
    # service before the Cloudflare Access app exists.
    if not expected_aud:
        return

    if settings.theoros_reader_allow_localhost_bypass:
        client_host = request.client.host if request.client else None
        if client_host in LOCALHOST_IPS:
            return

    if not cf_jwt:
        logger.warning(
            "rejected request from %s: missing Cf-Access-Jwt-Assertion",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(cf_jwt)
        payload: dict[str, Any] = jwt.decode(
            cf_jwt,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_aud,
            options={"require": ["exp", "iat", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        logger.warning("rejected request: JWT expired")
        raise HTTPException(status_code=403, detail="Unauthorized")
    except jwt.InvalidAudienceError:
        logger.warning("rejected request: JWT audience mismatch")
        raise HTTPException(status_code=403, detail="Unauthorized")
    except jwt.InvalidTokenError as exc:
        logger.warning("rejected request: invalid JWT (%s)", exc)
        raise HTTPException(status_code=403, detail="Unauthorized")

    # User-login JWTs put the identity in `email`.  Service-token JWTs
    # leave `email` empty and populate `common_name` instead — those
    # belong to machine callers and shouldn't reach the reader.
    email = payload.get("email", "")
    if not expected_email or email.lower() != expected_email.lower():
        logger.warning(
            "rejected request: email %r not allowed",
            email[:64] if email else "",
        )
        raise HTTPException(status_code=403, detail="Unauthorized")
