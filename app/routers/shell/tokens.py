"""Personal Access Token management (User → Settings → Tokens).

Any authenticated *human* (local or Cognito) may mint, list, and revoke their
own PATs. A PAT-authenticated request may list its siblings but may NOT create
or revoke tokens — that stops a leaked token from spawning more (see
`_require_human`). Tokens inherit the caller's current scopes, snapshotted at
creation; default 90-day / max 365-day lifetime. The raw token is shown exactly
once, at creation; only its hash is stored.
"""
from __future__ import annotations

import datetime
import logging
import secrets

from boto3.dynamodb.conditions import Key
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ... import pat
from ...aws import get_table
from ...config import get_settings
from ...security import Principal, get_principal

logger = logging.getLogger("cfins.tokens")

router = APIRouter(prefix="/me/tokens", tags=["tokens"])


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_human(principal: Principal) -> None:
    """Only human logins manage tokens — a PAT can't mint or revoke tokens."""
    if principal.provider == "pat":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Personal access tokens cannot manage tokens; sign in to continue",
        )


def _meta(item: dict) -> dict:
    """Public view of a token — never the hash or the raw value."""
    expires_at = item.get("expiresAt", "")
    return {
        "id": item.get("tokenId", ""),
        "name": item.get("name", ""),
        "description": item.get("description", ""),
        "scopes": list(item.get("scopes", [])),
        "createdAt": item.get("createdAt", ""),
        "expiresAt": expires_at,
        "last4": item.get("last4", ""),
        "expired": bool(expires_at) and _now_iso() >= str(expires_at),
    }


class TokenCreate(BaseModel):
    name: str
    description: str = ""
    expiresInDays: int | None = None


@router.post("", status_code=status.HTTP_201_CREATED)
def create_token(
    body: TokenCreate, principal: Principal = Depends(get_principal)
) -> dict:
    _require_human(principal)
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Token name is required")

    days = body.expiresInDays or pat.DEFAULT_TTL_DAYS
    if days < 1 or days > pat.MAX_TTL_DAYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"expiresInDays must be between 1 and {pat.MAX_TTL_DAYS}",
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    expires = now + datetime.timedelta(days=days)
    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    ttl_epoch = int(expires.timestamp())  # for a future DynamoDB TTL auto-purge

    raw = pat.new_raw_token()
    token_hash = pat.hash_token(raw)
    token_id = secrets.token_hex(8)
    env = get_settings().environment

    common = {
        "tokenId": token_id,
        "username": principal.username,
        "name": name,
        "description": body.description.strip(),
        "scopes": principal.scopes,  # snapshot of the caller's current scopes
        "env": env,
        "createdAt": created_at,
        "expiresAt": expires_at,
        "ttl": ttl_epoch,
    }
    list_item = {
        "pk": f"USER#{principal.username}",
        "sk": f"TOKEN#{token_id}",
        **common,
        "tokenHash": token_hash,  # lets revoke find the AUTH item
        "last4": raw[-4:],
    }
    auth_item = {
        "pk": f"PAT#{token_hash}",
        "sk": "TOKEN",
        **common,
        "email": principal.email,
        "displayName": principal.display_name,
        "groups": principal.groups,
    }

    table = get_table()
    # Write LIST first, AUTH last: the AUTH item is what makes the token *work*,
    # so we only hand out the raw token after it's fully persisted. A partial
    # write leaves at worst an orphan LIST item (visible, revocable, dead).
    table.put_item(Item=list_item)
    table.put_item(Item=auth_item)

    logger.info("PAT %s (%r) created for %s (expires %s)",
                token_id, name, principal.username, expires_at)
    return {"token": raw, **_meta(list_item)}


@router.get("")
def list_tokens(principal: Principal = Depends(get_principal)) -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(f"USER#{principal.username}")
        & Key("sk").begins_with("TOKEN#")
    )
    tokens = [_meta(i) for i in resp.get("Items", [])]
    tokens.sort(key=lambda t: t["createdAt"], reverse=True)
    return {"tokens": tokens}


@router.delete("/{token_id}")
def revoke_token(
    token_id: str, principal: Principal = Depends(get_principal)
) -> dict:
    _require_human(principal)
    table = get_table()
    resp = table.get_item(
        Key={"pk": f"USER#{principal.username}", "sk": f"TOKEN#{token_id}"}
    )
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found")

    token_hash = item.get("tokenHash")
    if token_hash:  # kill the AUTH item first — that's what makes it work
        table.delete_item(Key={"pk": f"PAT#{token_hash}", "sk": "TOKEN"})
    table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
    logger.info("PAT %s (%r) revoked by %s", token_id, item.get("name", ""), principal.username)
    return {"status": "revoked", "id": token_id}
