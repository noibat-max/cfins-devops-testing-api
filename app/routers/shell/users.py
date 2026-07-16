"""Local user administration (§10, local users only — not Cognito).

Local users are single-table items: pk="USERS", sk="USER#<username>", with
passwordHash (bcrypt), email, displayName, groups[], status, createdAt.

All routes require the `api/admin` scope. Admins manage *other* users here;
self-actions are rejected (you can't delete/relock your own account) — change
your own password via POST /auth/change-password instead.
"""
from __future__ import annotations

import datetime
import logging

import bcrypt
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ... import pat
from ...aws import get_table
from ...groups import get_group_scopes
from ...security import Principal, require_scopes

logger = logging.getLogger("cfins.admin.users")

router = APIRouter(tags=["users"])

MIN_PASSWORD_LEN = 8
VALID_STATUSES = {"active", "disabled"}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public(item: dict) -> dict:
    """User view without the password hash."""
    return {
        "username": item.get("username", ""),
        "email": item.get("email", ""),
        "displayName": item.get("displayName", ""),
        "groups": list(item.get("groups", [])),
        "status": item.get("status", "active"),
        "createdAt": item.get("createdAt", ""),
    }


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Password must be at least {MIN_PASSWORD_LEN} characters",
        )


def _validate_groups(groups: list[str]) -> None:
    known = set(get_group_scopes().keys())
    unknown = [g for g in groups if g not in known]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown group(s): {', '.join(unknown)}"
        )


def _get_user_item(username: str) -> dict:
    resp = get_table().get_item(Key={"pk": "USERS", "sk": f"USER#{username}"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return item


def _forbid_self(username: str, principal: Principal) -> None:
    if username == principal.username:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You cannot perform this action on your own account",
        )


class UserCreate(BaseModel):
    username: str
    password: str
    email: str = ""
    displayName: str = ""
    groups: list[str] = []
    status: str = "active"


class PasswordSet(BaseModel):
    password: str


class GroupsSet(BaseModel):
    groups: list[str]


class UserUpdate(BaseModel):
    # Partial profile update — only provided fields change. Password is separate.
    email: str | None = None
    displayName: str | None = None
    status: str | None = None
    groups: list[str] | None = None


@router.get("/users", dependencies=[Depends(require_scopes("api/admin"))])
def list_users() -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq("USERS") & Key("sk").begins_with("USER#")
    )
    users = [_public(i) for i in resp.get("Items", [])]
    users.sort(key=lambda u: u["username"])
    return {"users": users}


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("api/admin"))],
)
def create_user(body: UserCreate) -> dict:
    username = body.username.strip()
    if not username or " " in username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid username")
    _validate_password(body.password)
    _validate_groups(body.groups)
    if body.status not in VALID_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status")

    item = {
        "pk": "USERS",
        "sk": f"USER#{username}",
        "username": username,
        "passwordHash": _hash(body.password),
        "email": body.email,
        "displayName": body.displayName,
        "groups": body.groups,
        "status": body.status,
        "createdAt": _now(),
    }
    try:
        get_table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status.HTTP_409_CONFLICT, "User already exists")
        raise
    logger.info("user %s created (groups: %s, status: %s)",
                username, ",".join(body.groups) or "(none)", body.status)
    return _public(item)


@router.get(
    "/users/{username}",
    dependencies=[Depends(require_scopes("api/admin"))],
)
def get_user(username: str) -> dict:
    return _public(_get_user_item(username))


@router.put("/users/{username}/password")
def set_user_password(
    username: str,
    body: PasswordSet,
    principal: Principal = Depends(require_scopes("api/admin")),
) -> dict:
    _forbid_self(username, principal)  # change your own via /auth/change-password
    _get_user_item(username)  # 404 if missing
    _validate_password(body.password)
    get_table().update_item(
        Key={"pk": "USERS", "sk": f"USER#{username}"},
        UpdateExpression="SET passwordHash = :h",
        ExpressionAttributeValues={":h": _hash(body.password)},
    )
    logger.info("user %s password reset by %s", username, principal.username)
    return {"status": "password updated", "username": username}


@router.patch("/users/{username}")
def update_user(
    username: str,
    body: UserUpdate,
    principal: Principal = Depends(require_scopes("api/admin")),
) -> dict:
    _forbid_self(username, principal)  # own account not editable here
    _get_user_item(username)  # 404 if missing

    provided = body.model_dump(exclude_unset=True)
    if not provided:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if "status" in provided and provided["status"] not in VALID_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status")
    if "groups" in provided:
        _validate_groups(provided["groups"])

    # Alias every attribute (avoids DynamoDB reserved words like `status`).
    set_parts: list[str] = []
    names: dict[str, str] = {}
    values: dict = {}
    for field, value in provided.items():
        names[f"#{field}"] = field
        set_parts.append(f"#{field} = :{field}")
        values[f":{field}"] = value

    get_table().update_item(
        Key={"pk": "USERS", "sk": f"USER#{username}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    # Disabling a user kills their PATs too (PAT auth doesn't re-check status).
    if provided.get("status") == "disabled":
        pat.revoke_all_for_user(username)
    logger.info("user %s updated (%s)", username, ", ".join(provided.keys()))
    return _public(_get_user_item(username))


@router.put("/users/{username}/groups")
def set_user_groups(
    username: str,
    body: GroupsSet,
    principal: Principal = Depends(require_scopes("api/admin")),
) -> dict:
    _forbid_self(username, principal)  # don't let admins relock themselves out
    _get_user_item(username)
    _validate_groups(body.groups)
    get_table().update_item(
        Key={"pk": "USERS", "sk": f"USER#{username}"},
        UpdateExpression="SET groups = :g",
        ExpressionAttributeValues={":g": body.groups},
    )
    logger.info("user %s groups set to [%s] by %s",
                username, ",".join(body.groups) or "(none)", principal.username)
    return {"status": "groups updated", "username": username, "groups": body.groups}


@router.delete("/users/{username}")
def delete_user(
    username: str,
    principal: Principal = Depends(require_scopes("api/admin")),
) -> dict:
    _forbid_self(username, principal)  # can't delete your own account
    revoked = pat.revoke_all_for_user(username)  # tokens die with the user
    get_table().delete_item(Key={"pk": "USERS", "sk": f"USER#{username}"})
    logger.info("user %s deleted by %s (%d token(s) revoked)", username, principal.username, revoked)
    return {"status": "user deleted", "username": username, "tokensRevoked": revoked}
