"""Cloudflare Access JWT validation for the capture service.

Cloudflare Zero Trust signs every authenticated request with a JWT in
the Cf-Access-Jwt-Assertion header. Cloudflare strips the raw
CF-Access-Client-Id/Secret headers before forwarding (they identify
the caller to Cloudflare but aren't meant for the origin), so the JWT
is the only thing the origin sees that proves authentication.

We verify the JWT signature against Cloudflare's published RSA public
keys, check that the audience matches our Access application, and
read the service token's Client ID out of the `common_name` claim.
That value must match THEOROS_CF_ACCESS_CLIENT_ID from .env.

The signature check is the load-bearing part: it proves the JWT was
issued by *this account's* Cloudflare Access, not forged. The
audience check binds the JWT to *this specific application* (so a JWT
issued for one hostname can't be replayed against another). The
common_name check binds it to *this
service token* (so other tokens we might issue later for other
purposes don't accidentally get capture access).

Localhost requests bypass the check by default — the browser
extension and native daemons talk directly to 127.0.0.1:8765 without
going through Cloudflare and have no JWT to send.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from fastapi import Header, HTTPException, Request
from jwt import PyJWKClient

from .config import CaptureSettings

logger = logging.getLogger(__name__)

settings = CaptureSettings()

LOCALHOST_IPS = {"127.0.0.1", "::1"}

# Cloudflare publishes a JWKS (JSON Web Key Set) for each Zero Trust
# team at https://<team-domain>/cdn-cgi/access/certs. Team domain and
# app AUD both come from the environment so this file carries no
# account-specific identifiers. PyJWKClient caches keys in memory and
# refreshes on cache miss, so key rotation is transparent.
CF_TEAM_DOMAIN = settings.theoros_cf_team_domain
CF_AUD = settings.theoros_cf_aud

# Lazily constructed on first use so that a missing team domain in
# local dev doesn't blow up at import time.
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if not CF_TEAM_DOMAIN:
            raise RuntimeError(
                "THEOROS_CF_TEAM_DOMAIN is not set but Cloudflare Access auth is active."
            )
        _jwks_client = PyJWKClient(
            f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs"
        )
    return _jwks_client


async def require_cf_access(
    request: Request,
    cf_jwt: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> None:
    """FastAPI dependency: validate Cloudflare Access JWT.

    Returns None on success, raises HTTPException(403) on failure.
    """
    expected_client_id = settings.theoros_cf_access_client_id

    # Auth disabled (no expected Client ID configured): accept everything.
    if not expected_client_id:
        return

    # Localhost bypass: requests from the local machine never went
    # through Cloudflare and shouldn't be expected to carry the JWT.
    if settings.theoros_cf_allow_localhost_bypass:
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
        # Get the signing key for this JWT's key ID. PyJWKClient
        # handles the fetch-and-cache from the JWKS URL.
        signing_key = _get_jwks_client().get_signing_key_from_jwt(cf_jwt)

        # Verify signature, audience, and expiry in one call.
        # PyJWT raises on any failure.
        payload: dict[str, Any] = jwt.decode(
            cf_jwt,
            signing_key.key,
            algorithms=["RS256"],
            audience=CF_AUD,
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

    # common_name on a service-token JWT is the Client ID (e.g.,
    # "fe10c2ee...access"). On a user-login JWT it would be empty
    # and the email claim would be populated instead. We require
    # service-token auth specifically.
    common_name = payload.get("common_name", "")
    if common_name != expected_client_id:
        logger.warning(
            "rejected request: common_name %r != expected %r",
            common_name[:16] + "..." if common_name else "",
            expected_client_id[:16] + "...",
        )
        raise HTTPException(status_code=403, detail="Unauthorized")

    # Authenticated. No return value; the dependency's purpose is the
    # side effect of refusing unauthorized requests.
