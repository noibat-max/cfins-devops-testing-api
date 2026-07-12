"""Personal Access Tokens (PATs) — opaque bearer tokens for the CLI.

A PAT is a high-entropy opaque string prefixed ``qapat_``. We never store the raw
token — only its SHA-256 hash. Two single-table items back each token:

  * AUTH item  pk="PAT#<sha256>"  sk="TOKEN"        → O(1) lookup at auth time
  * LIST item  pk="USER#<user>"   sk="TOKEN#<id>"   → list / revoke by owner

The LIST item also carries the hash, so revoke can delete the AUTH item without
ever seeing the raw token. Scopes are SNAPSHOTTED at creation (frozen power);
revocation and expiry are the controls. Each token is stamped with the API's
``env`` so a token minted in one environment is rejected in another (defence in
depth on top of the per-environment table separation). See CLAUDE.md §5.
"""
from __future__ import annotations

import hashlib
import secrets

from boto3.dynamodb.conditions import Key

from .aws import get_table

PAT_PREFIX = "qapat_"
DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 365


def new_raw_token() -> str:
    """A fresh opaque token: ``qapat_`` + 256 bits of URL-safe entropy."""
    return PAT_PREFIX + secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """The SHA-256 hex digest we store and look up by (never the raw token)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def get_auth_item(raw: str) -> dict | None:
    """The AUTH item for a presented raw token, or None if unknown."""
    resp = get_table().get_item(Key={"pk": f"PAT#{hash_token(raw)}", "sk": "TOKEN"})
    return resp.get("Item")


def revoke_all_for_user(username: str) -> int:
    """Delete every PAT owned by a user (both items). Returns how many.

    Called when a user is deleted or disabled so their tokens die with them —
    PAT auth doesn't re-check user status on the hot path, so this is the
    control that stops a disabled user's token from still working.
    """
    table = get_table()
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"USER#{username}")
        & Key("sk").begins_with("TOKEN#")
    )
    items = resp.get("Items", [])
    for it in items:
        token_hash = it.get("tokenHash")
        if token_hash:  # kill the AUTH item first — that's what makes it work
            table.delete_item(Key={"pk": f"PAT#{token_hash}", "sk": "TOKEN"})
        table.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
    return len(items)
