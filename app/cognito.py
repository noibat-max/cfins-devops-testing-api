"""AWS Cognito access-token validation + identity lookup.

The SPA sends a Cognito **access token** as its Bearer. Access tokens are
authorization-only: they carry `cognito:groups` (→ our scopes) but no email/name.
So this module does two things:

  1. validate_access_token(token) — verify the RS256 signature against the pool's
     public JWKS, plus issuer / expiry / token_use=access / client_id.
  2. fetch_userinfo(token, sub)  — GET the OIDC /oauth2/userInfo endpoint (with the
     same access token) to obtain email/name, cached per-sub with a short TTL.

No AWS credentials are used here — JWKS and userInfo are public HTTPS endpoints
authorized by the token itself.
"""
from __future__ import annotations

import functools
import json
import ssl
import time
import urllib.request

import certifi
import jwt
from jwt import PyJWKClient

from .config import get_settings

_USERINFO_TTL_SECONDS = 300.0
# sub -> (fetched_at, {email, name, ...})
_userinfo_cache: dict[str, tuple[float, dict]] = {}


@functools.lru_cache
def _ssl_context() -> ssl.SSLContext:
    """TLS context using certifi's CA bundle.

    Explicit so verification works regardless of the interpreter's default cert
    store — notably python.org macOS builds, which don't use the system store.
    Same code path works in the Linux container.
    """
    return ssl.create_default_context(cafile=certifi.where())


@functools.lru_cache
def _jwk_client() -> PyJWKClient:
    """One JWKS client for the pool; PyJWKClient caches signing keys internally."""
    return PyJWKClient(get_settings().cognito_jwks_url, ssl_context=_ssl_context())


def validate_access_token(token: str) -> dict:
    """Verify a Cognito access token and return its claims. Raises on any failure."""
    settings = get_settings()
    signing_key = _jwk_client().get_signing_key_from_jwt(token)

    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=settings.cognito_issuer,
        # Access tokens have no `aud`; we check `client_id` ourselves below.
        options={"verify_aud": False},
    )

    if claims.get("token_use") != "access":
        raise jwt.InvalidTokenError("token_use is not 'access'")
    if claims.get("client_id") != settings.cognito_client_id:
        raise jwt.InvalidTokenError("client_id does not match this app client")

    return claims


def fetch_userinfo(access_token: str, sub: str) -> dict:
    """Identity (email/name) from Cognito's userInfo endpoint, cached per sub.

    Returns {} on any failure — identity is best-effort; authorization never
    depends on it.
    """
    now = time.time()
    hit = _userinfo_cache.get(sub)
    if hit and (now - hit[0]) < _USERINFO_TTL_SECONDS:
        return hit[1]

    url = get_settings().cognito_userinfo_url
    if not url:
        return {}

    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 (fixed https host)
            req, timeout=5, context=_ssl_context()
        ) as resp:
            data = json.load(resp)
    except Exception:  # noqa: BLE001 — degrade gracefully, keep authz working
        return {}

    if isinstance(data, dict):
        _userinfo_cache[sub] = (now, data)
        return data
    return {}
