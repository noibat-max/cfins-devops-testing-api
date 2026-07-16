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

import datetime
import logging
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import cognito, pat
from .config import get_settings
from .groups import resolve_scopes
from .logging_config import set_log_user

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
    # How this request authenticated: "local"/"cognito" (a human JWT) or "pat"
    # (a Personal Access Token). Gates token management to human logins.
    provider: str = "local"


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # PATs are opaque (not JWTs) so they can't be routed by `iss`; catch them
    # first by their `qapat_` prefix, before the JWT decode path.
    if token.startswith(pat.PAT_PREFIX):
        principal = _principal_from_pat(token)
    else:
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

        principal = Principal(
            username=username,
            email=email,
            display_name=display_name,
            groups=groups,
            scopes=resolve_scopes(groups),  # per-request, from the cached mapping
            provider=provider,
        )

    # Bind the identity for logging (keyed by correlation id, so every log line
    # for this request — router lines and the summary — carries the user).
    set_log_user(principal.username)
    return principal


def _principal_from_pat(token: str) -> Principal:
    """Authenticate an opaque PAT: hash → lookup → expiry → env stamp.

    Unlike the JWT paths, a PAT's scopes are the SNAPSHOT taken at creation
    (frozen power), NOT re-resolved from groups. Any failure is a generic 401.
    """
    item = pat.get_auth_item(token)
    if not item:
        raise _UNAUTHENTICATED

    expires_at = item.get("expiresAt")
    # Fixed-format UTC ISO strings compare chronologically as plain strings.
    if expires_at and _utcnow_iso() >= str(expires_at):
        raise _UNAUTHENTICATED

    token_env = item.get("env")
    if token_env and token_env != get_settings().environment:
        logger.debug(
            "PAT env mismatch: token=%r api=%r", token_env, get_settings().environment
        )
        raise _UNAUTHENTICATED

    return Principal(
        username=item.get("username", ""),
        email=item.get("email", ""),
        display_name=item.get("displayName", ""),
        groups=list(item.get("groups", [])),
        scopes=list(item.get("scopes", [])),  # snapshot — NOT resolve_scopes
        provider="pat",
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
