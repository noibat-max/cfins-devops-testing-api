"""Authentication routes.

Phase 3: POST /auth/login — verify a local (DB-backed) username/password with
bcrypt, mint an HS256 JWT carrying the user's groups, and return the token plus
the user's resolved scopes (for UI gating). Failures return a generic 401 so we
never leak whether a username exists.

GET /auth/me and the scope-enforcing middleware arrive in Phase 4.
"""
from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..aws import get_table
from ..groups import resolve_scopes
from ..security import Principal, get_principal
from ..tokens import mint_token

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid username or password",
)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    username: str
    email: str
    displayName: str
    groups: list[str]
    scopes: list[str]


class LoginResponse(BaseModel):
    token: str
    user: UserOut


def _get_user(username: str) -> dict | None:
    resp = get_table().get_item(Key={"pk": "USERS", "sk": f"USER#{username}"})
    return resp.get("Item")


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    user = _get_user(body.username)

    # Uniform failure path: unknown user, inactive user, or bad password all 401.
    if not user or user.get("status") != "active":
        raise _INVALID

    stored_hash = user.get("passwordHash", "")
    if not stored_hash or not bcrypt.checkpw(
        body.password.encode(), stored_hash.encode()
    ):
        raise _INVALID

    groups = list(user.get("groups", []))
    scopes = resolve_scopes(groups)

    token = mint_token(
        username=user["username"],
        email=user.get("email", ""),
        display_name=user.get("displayName", ""),
        groups=groups,
    )

    return LoginResponse(
        token=token,
        user=UserOut(
            username=user["username"],
            email=user.get("email", ""),
            displayName=user.get("displayName", ""),
            groups=groups,
            scopes=scopes,
        ),
    )


@router.get("/me", response_model=UserOut)
def me(principal: Principal = Depends(get_principal)) -> UserOut:
    """Identity + freshly-resolved scopes for the bearer token. Requires auth."""
    return UserOut(
        username=principal.username,
        email=principal.email,
        displayName=principal.display_name,
        groups=principal.groups,
        scopes=principal.scopes,
    )
