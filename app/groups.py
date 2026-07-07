"""Group → scope resolution (Option 2, data-driven + cached).

The group→scope mapping is the source of truth in DynamoDB (items pk="GROUPS",
sk="GROUP#<name>"), NOT hardcoded. We cache it in-memory with a short TTL so
DynamoDB is never in the request hot path, yet edits propagate within the TTL
for everyone with no re-login.

`resolve_scopes(groups)` maps a token's groups claim to the union of their
scopes. Used by the login endpoint (to return scopes for UI gating) and by the
Phase 4 authZ middleware (to enforce them per request).
"""
from __future__ import annotations

import time

from boto3.dynamodb.conditions import Key

from .aws import get_table

# ~5 min TTL: fresh enough for admin edits, cheap enough to keep DynamoDB cold.
_TTL_SECONDS = 300.0
_cache: dict[str, object] = {"data": None, "loaded_at": 0.0}


def _load_from_db() -> dict[str, list[str]]:
    """Read all GROUP# items and build {group_name: [scopes]}."""
    table = get_table()
    resp = table.query(KeyConditionExpression=Key("pk").eq("GROUPS"))
    return {
        item["name"]: list(item.get("scopes", []))
        for item in resp.get("Items", [])
        if "name" in item
    }


def get_group_scopes(*, force: bool = False) -> dict[str, list[str]]:
    """The cached group→scopes mapping, refreshed when the TTL expires."""
    now = time.time()
    stale = _cache["data"] is None or (now - float(_cache["loaded_at"])) > _TTL_SECONDS
    if force or stale:
        _cache["data"] = _load_from_db()
        _cache["loaded_at"] = now
    return _cache["data"]  # type: ignore[return-value]


def resolve_scopes(groups: list[str]) -> list[str]:
    """Union of scopes across the given groups (unknown groups contribute none)."""
    mapping = get_group_scopes()
    scopes: set[str] = set()
    for g in groups:
        scopes.update(mapping.get(g, []))
    return sorted(scopes)
