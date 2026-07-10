"""Group administration — manage groups and their scope assignments.

Groups are single-table items: pk="GROUPS", sk="GROUP#<name>", with scopes[] and
description. The group→scope mapping is the source of truth for authorization;
edits bust the in-memory cache so they take effect immediately.

Scopes themselves are fixed by the code (see app/scopes.py) — you assign existing
scopes to groups, you don't invent new ones.

All routes require `api/admin`.
"""
from __future__ import annotations

import datetime

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..aws import get_table
from ..groups import bust_cache
from ..scopes import ADMIN_SCOPE, SCOPE_CATALOG, VALID_SCOPES
from ..security import require_scopes

router = APIRouter(tags=["groups"])

ADMIN_GROUP = "admin"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public(item: dict) -> dict:
    return {
        "name": item.get("name", ""),
        "description": item.get("description", ""),
        "scopes": list(item.get("scopes", [])),
    }


def _validate_scopes(scopes: list[str]) -> None:
    unknown = [s for s in scopes if s not in VALID_SCOPES]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown scope(s): {', '.join(unknown)}"
        )


def _get_group_item(name: str) -> dict:
    resp = get_table().get_item(Key={"pk": "GROUPS", "sk": f"GROUP#{name}"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
    return item


def _members(name: str) -> list[str]:
    """Usernames currently in a group (for lockout / in-use safeguards)."""
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq("USERS") & Key("sk").begins_with("USER#")
    )
    return [
        u.get("username", "")
        for u in resp.get("Items", [])
        if name in u.get("groups", [])
    ]


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    scopes: list[str] = []


class GroupUpdate(BaseModel):
    description: str | None = None
    scopes: list[str] | None = None


@router.get("/scopes", dependencies=[Depends(require_scopes("api/admin"))])
def list_scopes() -> dict:
    return {"scopes": SCOPE_CATALOG}


@router.get("/groups", dependencies=[Depends(require_scopes("api/admin"))])
def list_groups() -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq("GROUPS") & Key("sk").begins_with("GROUP#")
    )
    groups = [_public(g) for g in resp.get("Items", [])]
    groups.sort(key=lambda g: g["name"])
    return {"groups": groups}


@router.post(
    "/groups",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("api/admin"))],
)
def create_group(body: GroupCreate) -> dict:
    name = body.name.strip()
    if not name or " " in name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid group name")
    _validate_scopes(body.scopes)

    item = {
        "pk": "GROUPS",
        "sk": f"GROUP#{name}",
        "name": name,
        "description": body.description,
        "scopes": body.scopes,
        "createdAt": _now(),
    }
    try:
        get_table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status.HTTP_409_CONFLICT, "Group already exists")
        raise
    bust_cache()
    return _public(item)


@router.patch("/groups/{name}", dependencies=[Depends(require_scopes("api/admin"))])
def update_group(name: str, body: GroupUpdate) -> dict:
    _get_group_item(name)
    provided = body.model_dump(exclude_unset=True)
    if not provided:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    if "scopes" in provided:
        _validate_scopes(provided["scopes"])
        # Lockout guard: the admin group must keep the wildcard scope.
        if name == ADMIN_GROUP and ADMIN_SCOPE not in provided["scopes"]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"The '{ADMIN_GROUP}' group must retain the {ADMIN_SCOPE} scope",
            )

    set_parts, names, values = [], {}, {}
    for field, value in provided.items():
        names[f"#{field}"] = field
        set_parts.append(f"#{field} = :{field}")
        values[f":{field}"] = value

    get_table().update_item(
        Key={"pk": "GROUPS", "sk": f"GROUP#{name}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    bust_cache()
    return _public(_get_group_item(name))


@router.delete("/groups/{name}", dependencies=[Depends(require_scopes("api/admin"))])
def delete_group(name: str) -> dict:
    if name == ADMIN_GROUP:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"The '{ADMIN_GROUP}' group cannot be deleted"
        )
    members = _members(name)
    if members:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Group is in use by {len(members)} user(s): {', '.join(members[:5])}"
            + ("…" if len(members) > 5 else ""),
        )
    get_table().delete_item(Key={"pk": "GROUPS", "sk": f"GROUP#{name}"})
    bust_cache()
    return {"status": "group deleted", "name": name}
