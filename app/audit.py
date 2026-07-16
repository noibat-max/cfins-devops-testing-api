"""Audit trail — record every mutating request for governance review.

An `AuditMiddleware` (pure ASGI, like RequestLogMiddleware) captures every
POST/PUT/PATCH/DELETE — actor, method, path, query, request body, response
status, client IP, correlation id — and writes it to DynamoDB. Reads (GET) are
not audited. Recording happens AFTER the response is sent, off the request's
latency path, in a threadpool, best-effort (a failed audit write never fails or
slows the request).

Storage (single-table, fan-out so both review queries are direct partition
Queries — see docs / CLAUDE.md):
  Global    pk="AUDIT"           sk="<ts>#<id>"   → actions in a time window (all users)
  Per-user  pk="AUDIT#<actor>"   sk="<ts>#<id>"   → what a given user did

Secrets never land in the trail: passwords/tokens are always redacted, and the
`value` field is redacted on `/secrets` paths (so variable/header values stay
visible but secret values don't).
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import uuid

from boto3.dynamodb.conditions import Key

from .aws import get_table
from .config import get_settings
from .logging_config import get_correlation_id, get_log_user

logger = logging.getLogger("cfins.audit")

AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
TTL_DAYS = 365
BODY_CAP = 8192  # 8 KB — truncate larger payloads

# Keys whose values are always redacted, anywhere in the body (case-insensitive).
SENSITIVE_KEYS = {"password", "current_password", "new_password", "passwordhash", "token"}
_UNSET = "-"


# ------------------------------------------------------------- redaction ---
def _redact(obj, redact_value: bool):
    """Recursively replace sensitive values with a marker. `redact_value` also
    redacts a `value` field (used only on /secrets paths)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in SENSITIVE_KEYS or (redact_value and kl == "value"):
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact(v, redact_value)
        return out
    if isinstance(obj, list):
        return [_redact(v, redact_value) for v in obj]
    return obj


def _prepare_body(parsed, raw: bytes, path: str) -> str:
    """Redacted, size-capped JSON string of the request body for storage."""
    if parsed is None:
        return "" if not raw else "<non-JSON body>"
    red = _redact(parsed, redact_value="/secrets" in path)
    s = json.dumps(red, default=str, separators=(",", ":"))
    if len(s) > BODY_CAP:
        s = s[:BODY_CAP] + "…(truncated)"
    return s


def _derive_action(method: str, path: str) -> str:
    if path.endswith("/login") or path.endswith("/auth/sso"):
        return "login"
    if path.endswith("/change-password"):
        return "change-password"
    return {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}.get(method, method.lower())


# --------------------------------------------------------------- record ---
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_event(*, actor: str, method: str, path: str, query: str,
                 body: str, status: int, ip: str, cid: str, action: str) -> None:
    """Fan-out write: one global item + one per-user item (best-effort)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    eid = uuid.uuid4().hex
    actor = actor or _UNSET
    common = {
        "sk": f"{ts}#{eid}",
        "id": eid,
        "timestamp": ts,
        "actor": actor,
        "method": method,
        "path": path,
        "action": action,
        "query": query,
        "body": body,
        "status": status,
        "outcome": "success" if 200 <= status < 400 else "failure",
        "ip": ip,
        "correlationId": cid,
        "env": get_settings().environment,
        "ttl": int((now + datetime.timedelta(days=TTL_DAYS)).timestamp()),
    }
    table = get_table()
    table.put_item(Item={**common, "pk": "AUDIT"})               # time-window index
    table.put_item(Item={**common, "pk": f"AUDIT#{actor}"})      # per-user index


# ---------------------------------------------------------------- query ---
def query_audit(*, user: str | None, start: str | None, end: str | None,
                limit: int, cursor: dict | None):
    """Query one partition: per-user if `user` given, else the global partition.

    Time window via the timestamp-prefixed sort key (`sk BETWEEN`). Newest first.
    Returns (items, last_evaluated_key).
    """
    pk = f"AUDIT#{user}" if user else "AUDIT"
    kc = Key("pk").eq(pk)
    if start and end:
        kc = kc & Key("sk").between(start, end + "￿")
    elif start:
        kc = kc & Key("sk").gte(start)
    elif end:
        kc = kc & Key("sk").lte(end + "￿")

    kwargs: dict = {"KeyConditionExpression": kc, "ScanIndexForward": False, "Limit": limit}
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor
    resp = get_table().query(**kwargs)
    return resp.get("Items", []), resp.get("LastEvaluatedKey")


def encode_cursor(lek: dict | None) -> str | None:
    if not lek:
        return None
    return base64.urlsafe_b64encode(json.dumps(lek, default=str).encode()).decode()


def decode_cursor(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------- middleware ---
def _client_ip(scope) -> str:
    # Prefer X-Forwarded-For (first hop) when behind an ALB/proxy, else the peer.
    for k, v in scope.get("headers", []):
        if k == b"x-forwarded-for":
            return v.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else _UNSET


class AuditMiddleware:
    """Capture every mutating request into the audit trail."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method", "") not in AUDIT_METHODS:
            await self.app(scope, receive, send)
            return

        # Buffer the body so we can both forward it downstream and audit it.
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                more = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                more = False

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        status_holder = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, replay_receive, send_wrapper)
        finally:
            # Response is already sent; record off the latency path, in a thread
            # (boto3 is blocking), best-effort — never surface an audit failure.
            try:
                method = scope.get("method", "")
                path = scope.get("path", "")
                parsed = None
                if body:
                    try:
                        parsed = json.loads(body)
                    except (ValueError, TypeError):
                        parsed = None
                # Actor: the auth-resolved user, else a `username` from the body
                # (covers login, where no principal is resolved).
                actor = get_log_user()
                if (not actor or actor == _UNSET) and isinstance(parsed, dict):
                    actor = parsed.get("username") or _UNSET
                await asyncio.to_thread(
                    record_event,
                    actor=actor,
                    method=method,
                    path=path,
                    query=scope.get("query_string", b"").decode("latin-1"),
                    body=_prepare_body(parsed, body, path),
                    status=status_holder["code"] or 500,
                    ip=_client_ip(scope),
                    cid=get_correlation_id(),
                    action=_derive_action(method, path),
                )
            except Exception:  # noqa: BLE001 — audit is best-effort
                logger.warning("audit record failed", exc_info=True)
