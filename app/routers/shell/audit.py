"""Audit log read API (workbench governance — admins only).

The trail is written by `app.audit.AuditMiddleware` on every mutating request.
This exposes the two review queries:
  * a time window across all users   (no `user`)
  * everything a given user did      (`user=<username>`, optionally windowed)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi import HTTPException

from ... import audit
from ...security import require_scopes
from ...serialization import to_jsonable

router = APIRouter(tags=["audit"])

# Fields returned to the UI (drop internal pk/sk; keep the reviewable content).
_VIEW = (
    "id", "timestamp", "actor", "action", "method", "path", "query",
    "body", "status", "outcome", "ip", "correlationId", "env",
)


@router.get("/audit", dependencies=[Depends(require_scopes("api/admin"))])
def list_audit(
    user: str | None = Query(None, description="Filter to one actor's actions."),
    start: str | None = Query(None, alias="from", description="ISO start (inclusive)."),
    end: str | None = Query(None, alias="to", description="ISO end (inclusive)."),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="Opaque page cursor from a prior call."),
) -> dict:
    start_key = audit.decode_cursor(cursor)
    if cursor and start_key is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor")

    items, lek = audit.query_audit(
        user=user, start=start, end=end, limit=limit, cursor=start_key
    )
    view = [to_jsonable({k: it[k] for k in _VIEW if k in it}) for it in items]
    return {"items": view, "nextCursor": audit.encode_cursor(lek)}
