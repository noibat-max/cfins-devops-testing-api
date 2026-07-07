"""Authentication + authorization dependencies (Option 2).

Per request:
  1. pull the Bearer token
  2. route by issuer — local HS256 now; Cognito RS256/JWKS is a deferred seam
  3. read the *groups* claim and resolve scopes from the in-memory cached mapping
  4. `require_scopes(...)` compares resolved scopes to what the endpoint declares
     (`api/admin` inherits everything)

Both providers land on the same Principal with the same claim handling, so authZ
is identical regardless of how the user logged in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import cognito
from .config import get_settings
from .groups import resolve_scopes

logger = logging.getLogger("cfins.auth")

# Wildcard scope: admins bypass per-endpoint scope checks.
ADMIN_SCOPE = "api/admin"

# auto_error=False so we can return a consistent 401 (HTTPBearer's default is 403).
_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass
class Principal:
    """The authenticated caller, with scopes already resolved for this request."""

    username: str
    email: str
    display_name: str
    groups: list[str]
    scopes: list[str]


def _decode(token: str) -> tuple[dict, str]:
    """Validate a token and return (claims, provider), routing by issuer.

    provider is "local" (our HS256 tokens) or "cognito" (Cognito RS256 tokens).
    """
    settings = get_settings()

    # Peek at the (unverified) issuer to choose the validation path.
    try:
        issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        raise _UNAUTHENTICATED

    if issuer == settings.jwt_issuer:
        # Local provider: verify signature, issuer and expiry with our secret.
        try:
            return jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=["HS256"],
                issuer=settings.jwt_issuer,
            ), "local"
        except jwt.PyJWTError:
            raise _UNAUTHENTICATED

    if settings.cognito_enabled and issuer == settings.cognito_issuer:
        # Cognito provider: verify RS256 against the pool's JWKS.
        try:
            return cognito.validate_access_token(token), "cognito"
        except jwt.PyJWTError as e:
            # Reason (not the token) kept at debug for troubleshooting.
            logger.debug("cognito token rejected: %r", e)
            raise _UNAUTHENTICATED

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Unsupported token issuer: {issuer!r}",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """FastAPI dependency: authenticate the request and resolve scopes.

    Both providers converge on the same Principal. Only the claim names and the
    identity source differ:
      * local   — groups from `groups`; identity in the token itself.
      * cognito — groups from `cognito:groups`; identity via cached userInfo
                  (access tokens carry no email/name).
    """
    if creds is None or not creds.credentials:
        raise _UNAUTHENTICATED

    token = creds.credentials
    claims, provider = _decode(token)

    if provider == "cognito":
        groups = list(claims.get("cognito:groups", []))
        username = claims.get("username") or claims.get("sub", "")
        info = cognito.fetch_userinfo(token, claims.get("sub", ""))
        email = info.get("email", "")
        display_name = info.get("name", "") or username
    else:
        groups = list(claims.get("groups", []))
        username = claims.get("sub", "")
        email = claims.get("email", "")
        display_name = claims.get("name", "")

    return Principal(
        username=username,
        email=email,
        display_name=display_name,
        groups=groups,
        scopes=resolve_scopes(groups),  # per-request, from the cached mapping
    )


def require_scopes(*required: str):
    """Return a dependency that enforces the given scopes (admin inherits all)."""

    def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if ADMIN_SCOPE in principal.scopes:
            return principal
        missing = [s for s in required if s not in principal.scopes]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope(s): {', '.join(missing)}",
            )
        return principal

    return _dependency
