"""Local JWT minting (HS256).

Issues the tokens our own login endpoint hands out for local / backdoor users.
The token carries the user's identity and **groups** — scopes are NOT baked in;
they're resolved per-request from the groups claim (see app/groups.py) so that
mapping edits take effect within the cache TTL without re-login.

Cognito RS256 tokens are validated elsewhere (Phase 4 middleware); this module
only mints our own HS256 tokens.
"""
from __future__ import annotations

import datetime

import jwt

from .config import get_settings


def mint_token(*, username: str, email: str, display_name: str, groups: list[str]) -> str:
    settings = get_settings()
    if not settings.jwt_secret:
        # Guard: never sign with an empty key.
        raise RuntimeError("JWT_SECRET is not configured")

    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "iss": settings.jwt_issuer,
        "sub": username,
        "email": email,
        "name": display_name,
        "groups": groups,
        "iat": now,
        "exp": now + datetime.timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
