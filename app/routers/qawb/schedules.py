"""Scheduled execution — EventBridge Scheduler-backed schedules.

A schedule fires a **"fire" Lambda** (target) that creates a pending execution and
enqueues it to SQS; the dispatcher then launches it (throttled). AWS EventBridge
Scheduler owns the timer — we don't poll. Each user schedule is:
  * an EventBridge Scheduler resource (Name = <id>, in the app's schedule group), and
  * a DynamoDB metadata item  pk="SCHEDULES"  sk="SCHEDULE#<id>".
CRUD keeps the two in sync (Scheduler first, then DynamoDB; rollback on drift).

v1 kinds: `once` (`expression` = ISO-8601 datetime, stored UTC) and `rate`
(`expression` = "<n> <minutes|hours|days>"). Cron is deferred. `next_run` is
computed on read from the expression (Scheduler doesn't expose it).
"""
from __future__ import annotations

import datetime
import json
import re
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...aws import get_scheduler_client, get_table
from ...config import get_settings
from ...security import Principal, require_scopes
from ...serialization import to_jsonable

router = APIRouter(tags=["schedules"])

SCHEDULES_PK = "SCHEDULES"
VALID_TARGET_TYPES = {"usecase", "suite"}
VALID_KINDS = {"once", "rate"}
VALID_CAPTURE = {"screenshots", "full"}
_RATE_RE = re.compile(r"^\s*(\d+)\s+(minutes?|hours?|days?)\s*$")

FIELDS = (
    "scheduleId", "app", "targetType", "targetId", "kind", "expression",
    "timezone", "capture", "mode", "state", "label", "createdBy", "createdAt",
    "lastRunAt", "lastExecutionId",
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_z() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(sid: str) -> dict:
    return {"pk": SCHEDULES_PK, "sk": f"SCHEDULE#{sid}"}


def _require_scheduler() -> None:
    if not get_settings().scheduler_enabled:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "scheduling is not configured on this server (no fire-Lambda / target role)",
        )


def _validate_target(target_type: str, target_id: str) -> None:
    if target_type not in VALID_TARGET_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"targetType must be one of {sorted(VALID_TARGET_TYPES)}")
    pk, sk = ("USECASES", f"USECASE#{target_id}") if target_type == "usecase" \
        else ("TEST_SUITES", f"SUITE#{target_id}")
    if not get_table().get_item(Key={"pk": pk, "sk": sk}).get("Item"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{target_type} not found")


def _rate_delta(expression: str) -> datetime.timedelta:
    m = _RATE_RE.match(expression)
    if not m:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "rate expression must be '<n> minutes|hours|days' (e.g. '1 days')")
    n, unit = int(m.group(1)), m.group(2).rstrip("s")
    if n < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rate value must be >= 1")
    return {"minute": datetime.timedelta(minutes=n),
            "hour": datetime.timedelta(hours=n),
            "day": datetime.timedelta(days=n)}[unit]


def _parse_once(expression: str) -> datetime.datetime:
    try:
        dt = datetime.datetime.fromisoformat(expression.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "once expression must be an ISO-8601 datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _schedule_expression(kind: str, expression: str) -> tuple[str, str | None]:
    """Return (ScheduleExpression, ScheduleExpressionTimezone) for AWS."""
    if kind == "rate":
        _rate_delta(expression)  # validate
        n, unit = _RATE_RE.match(expression).group(1, 2)
        return f"rate({int(n)} {unit if unit.endswith('s') else unit + 's'})", None
    dt = _parse_once(expression)
    if dt <= _now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "once datetime must be in the future")
    return f"at({dt.strftime('%Y-%m-%dT%H:%M:%S')})", "UTC"


def _next_run(item: dict) -> str | None:
    """Compute the next fire time for display (Scheduler doesn't expose it)."""
    if item.get("state") != "enabled":
        return None
    kind, expr = item.get("kind"), item.get("expression", "")
    now = _now()
    if kind == "once":
        try:
            dt = _parse_once(expr)
        except HTTPException:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt > now else None
    if kind == "rate":
        try:
            delta = _rate_delta(expr)
        except HTTPException:
            return None
        base = item.get("createdAt")
        try:
            b = datetime.datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except (TypeError, ValueError):
            return None
        elapsed = (now - b).total_seconds()
        k = int(elapsed // delta.total_seconds()) + 1
        return (b + k * delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _project(item: dict) -> dict:
    out = to_jsonable({k: item[k] for k in FIELDS if k in item})
    out["nextRun"] = _next_run(item)
    return out


def _put_scheduler(sid: str, item: dict) -> None:
    """Create-or-update the EventBridge Scheduler resource for this schedule."""
    s = get_settings()
    expr, tz = _schedule_expression(item["kind"], item["expression"])
    kwargs = dict(
        Name=sid,
        GroupName=s.scheduler_group,
        ScheduleExpression=expr,
        State="ENABLED" if item["state"] == "enabled" else "DISABLED",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": s.scheduler_fire_lambda_arn,
            "RoleArn": s.scheduler_target_role_arn,
            "Input": json.dumps({"scheduleId": sid}),
        },
        # A one-off schedule deletes itself after it fires.
        ActionAfterCompletion="DELETE" if item["kind"] == "once" else "NONE",
    )
    if tz:
        kwargs["ScheduleExpressionTimezone"] = tz
    client = get_scheduler_client()
    try:
        client.create_schedule(**kwargs)
    except client.exceptions.ConflictException:
        client.update_schedule(**kwargs)


# --------------------------------------------------------------- models -------
class ScheduleCreate(BaseModel):
    targetType: str
    targetId: str
    kind: str
    expression: str
    capture: str | None = None
    label: str | None = None


class ScheduleUpdate(BaseModel):
    expression: str | None = None
    capture: str | None = None
    label: str | None = None
    state: str | None = None  # "enabled" | "disabled"


# --------------------------------------------------------------- routes -------
@router.post("/schedules", status_code=status.HTTP_201_CREATED)
def create_schedule(
    body: ScheduleCreate,
    principal: Principal = Depends(require_scopes("api/qawb/schedules.write")),
) -> dict:
    _require_scheduler()
    if body.kind not in VALID_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"kind must be one of {sorted(VALID_KINDS)}")
    _validate_target(body.targetType, body.targetId)
    capture = (body.capture or get_settings().runner_capture).lower()
    if capture not in VALID_CAPTURE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"capture must be one of {sorted(VALID_CAPTURE)}")
    _schedule_expression(body.kind, body.expression)  # validate before writing

    sid = str(uuid.uuid4())
    item = {
        **_key(sid),
        "scheduleId": sid, "app": "qawb",
        "targetType": body.targetType, "targetId": body.targetId,
        "kind": body.kind, "expression": body.expression, "timezone": "UTC",
        "capture": capture, "mode": "queued", "state": "enabled",
        "label": (body.label or "").strip(),
        "createdBy": principal.username, "createdAt": _now_z(),
    }
    # Scheduler first, then DynamoDB — roll back the Scheduler resource on DB failure.
    _put_scheduler(sid, item)
    try:
        get_table().put_item(Item=item)
    except ClientError:
        try:
            get_scheduler_client().delete_schedule(Name=sid, GroupName=get_settings().scheduler_group)
        except ClientError:
            pass
        raise
    return _project(item)


@router.get("/schedules")
def list_schedules(
    targetId: str | None = None,
    _: Principal = Depends(require_scopes("api/qawb/schedules.read")),
) -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(SCHEDULES_PK) & Key("sk").begins_with("SCHEDULE#")
    )
    items = resp.get("Items", [])
    if targetId:
        items = [i for i in items if i.get("targetId") == targetId]
    items.sort(key=lambda i: i.get("createdAt", ""), reverse=True)
    return {"schedules": [_project(i) for i in items]}


def _get_or_404(sid: str) -> dict:
    item = get_table().get_item(Key=_key(sid)).get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    return item


@router.get("/schedules/{schedule_id}")
def get_schedule(
    schedule_id: str,
    _: Principal = Depends(require_scopes("api/qawb/schedules.read")),
) -> dict:
    return _project(_get_or_404(schedule_id))


@router.patch("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: str,
    body: ScheduleUpdate,
    _: Principal = Depends(require_scopes("api/qawb/schedules.write")),
) -> dict:
    _require_scheduler()
    item = _get_or_404(schedule_id)
    if body.expression is not None:
        item["expression"] = body.expression
    if body.capture is not None:
        c = body.capture.lower()
        if c not in VALID_CAPTURE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"capture must be one of {sorted(VALID_CAPTURE)}")
        item["capture"] = c
    if body.label is not None:
        item["label"] = body.label.strip()
    if body.state is not None:
        if body.state not in ("enabled", "disabled"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "state must be 'enabled' or 'disabled'")
        item["state"] = body.state
    _schedule_expression(item["kind"], item["expression"])  # revalidate

    _put_scheduler(schedule_id, item)  # full replace, mirrors update_schedule semantics
    get_table().put_item(Item=item)
    return _project(item)


@router.delete("/schedules/{schedule_id}")
def delete_schedule(
    schedule_id: str,
    _: Principal = Depends(require_scopes("api/qawb/schedules.write")),
) -> dict:
    item = _get_or_404(schedule_id)
    try:
        get_scheduler_client().delete_schedule(Name=schedule_id, GroupName=get_settings().scheduler_group)
    except ClientError as e:
        # Already gone (e.g. a `once` that auto-deleted) — proceed to remove metadata.
        if "ResourceNotFound" not in e.response["Error"]["Code"]:
            raise
    get_table().delete_item(Key=_key(schedule_id))
    return {"status": "deleted", "scheduleId": schedule_id}
