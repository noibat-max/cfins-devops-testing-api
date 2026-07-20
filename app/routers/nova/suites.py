"""Test suites — authoring (Phase 1: CRUD + membership).

Ports §8 of the API surface (authoring half). A suite is a named, ordered
collection of use cases you later run as one batch. Single-table:
  * suite       pk="TEST_SUITES"   sk="SUITE#<id>"
  * membership  pk="SUITE#<id>"    sk="USECASE#<usecaseId>"   (deleted on cascade)

Deviations from sample-qa-studio (approved):
  * The per-suite `scope` field (sample's `suite:<name>` for per-suite authZ) is
    DROPPED — we authorize at the app level via api/nova/suite.read/write, like
    we dropped OAuth Clients. Suites are just name/description/tags.
  * Membership resolves use-case details (name/active) LIVE on read, so renames
    stay current and a deleted use case surfaces as `missing` rather than stale.

Execution (POST /execute, executions, artifacts) is Phase 2 — not here yet.
"""
from __future__ import annotations

import datetime
import logging
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...aws import get_s3_client, get_table
from ...config import get_settings
from ...security import Principal, require_scopes
from .executions import VALID_CAPTURE, _launch_ecs_task
from ...serialization import to_jsonable

logger = logging.getLogger("cfins.nova.suites")

router = APIRouter(tags=["suites"])

SUITES_PK = "TEST_SUITES"
SUITE_EXEC_INDEX = "suite-execution-index"

NAME_MIN, NAME_MAX = 3, 100
DESC_MAX = 500

# Execution status vocab (member executions are ordinary use-case executions).
EXEC_TERMINAL = {"completed", "failed", "stopped"}
# Fields surfaced for a member execution inside a suite run.
MEMBER_FIELDS = (
    "executionId", "usecaseId", "status", "startedAt", "endedAt", "errorMessage",
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _now_z() -> str:
    """Sortable UTC stamp matching executions.py (so member/suite runs align)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query_all(**kwargs) -> list[dict]:
    """Query, following pagination so we never silently truncate."""
    table = get_table()
    resp = table.query(**kwargs)
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items += resp.get("Items", [])
    return items


def _suite_sk(suite_id: str) -> str:
    return f"SUITE#{suite_id}"


def _membership_pk(suite_id: str) -> str:
    return f"SUITE#{suite_id}"


def _get_suite_item(suite_id: str) -> dict:
    resp = get_table().get_item(Key={"pk": SUITES_PK, "sk": _suite_sk(suite_id)})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test suite not found")
    return item


def _member_items(suite_id: str) -> list[dict]:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(_membership_pk(suite_id))
        & Key("sk").begins_with("USECASE#")
    )
    return sorted(resp.get("Items", []), key=lambda m: m.get("sort", 0))


def _member_count(suite_id: str) -> int:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(_membership_pk(suite_id))
        & Key("sk").begins_with("USECASE#"),
        Select="COUNT",
    )
    return int(resp.get("Count", 0))


# ---- suite-execution helpers (Phase 2) ------------------------------------

def _suite_exec_pk(suite_id: str) -> str:
    return f"SUITE_EXECUTION#{suite_id}"


def _suite_exec_items(suite_id: str) -> list[dict]:
    return _query_all(
        KeyConditionExpression=Key("pk").eq(_suite_exec_pk(suite_id))
        & Key("sk").begins_with("EXECUTION#")
    )


def _get_suite_exec_or_404(suite_id: str, se_id: str) -> dict:
    item = get_table().get_item(
        Key={"pk": _suite_exec_pk(suite_id), "sk": f"EXECUTION#{se_id}"}
    ).get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Suite execution not found")
    return item


def _member_executions(se_id: str) -> list[dict]:
    """All member use-case executions of a suite run, via the suite-execution GSI."""
    return _query_all(
        IndexName=SUITE_EXEC_INDEX,
        KeyConditionExpression=Key("suite_execution_id").eq(se_id),
    )


def _rollup(members: list[dict]) -> tuple[str, dict]:
    """Derive a suite run's overall status + counts LIVE from member statuses."""
    c = {"pending": 0, "executing": 0, "completed": 0, "failed": 0, "stopped": 0}
    for m in members:
        s = m.get("status", "pending")
        if s in c:
            c[s] += 1
    total = len(members)
    if total == 0:
        overall = "completed"
    elif c["pending"] == total:
        overall = "pending"
    elif c["pending"] + c["executing"] > 0:
        overall = "running"
    elif c["failed"] > 0:
        overall = "failed"
    elif c["stopped"] > 0:
        overall = "stopped"
    else:
        overall = "completed"
    counts = {
        "total": total, "completed": c["completed"], "failed": c["failed"],
        "stopped": c["stopped"], "running": c["executing"], "pending": c["pending"],
    }
    return overall, counts


def _delete_member_execution(member: dict) -> int:
    """Delete a member execution's step/artifact children (+ S3) and the record."""
    table = get_table()
    uc, eid = member["usecaseId"], member["executionId"]
    children = _query_all(KeyConditionExpression=Key("pk").eq(f"EXECUTION#{eid}"))
    s3 = get_s3_client()
    bucket = get_settings().artifacts_bucket
    deleted = 0
    for it in children:
        if it["sk"].startswith("ARTIFACT#") and it.get("s3Key"):
            try:
                s3.delete_object(Bucket=bucket, Key=it["s3Key"])
                deleted += 1
            except ClientError:
                pass  # best-effort; still remove the record
        table.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
    table.delete_item(Key={"pk": f"USECASE_EXECUTION#{uc}", "sk": f"EXECUTION#{eid}"})
    return deleted


def _delete_suite_execution(suite_id: str, se_id: str) -> None:
    """Cascade a suite run: member executions (+ children + S3), then the record."""
    for m in _member_executions(se_id):
        _delete_member_execution(m)
    get_table().delete_item(
        Key={"pk": _suite_exec_pk(suite_id), "sk": f"EXECUTION#{se_id}"}
    )


def _validate_suite_fields(name: str | None, description: str | None) -> None:
    if name is not None:
        n = name.strip()
        if len(n) < NAME_MIN or len(n) > NAME_MAX:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"name must be between {NAME_MIN} and {NAME_MAX} characters",
            )
    if description is not None and len(description) > DESC_MAX:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"description must be {DESC_MAX} characters or less",
        )


# ---- request models -------------------------------------------------------

class SuiteCreate(BaseModel):
    name: str = ""
    description: str = ""
    tags: list[str] = []


class SuiteUpdate(BaseModel):
    # All optional — only provided fields are changed (partial update under PUT).
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class AddUsecasesRequest(BaseModel):
    usecaseIds: list[str] = []


# ---- suite CRUD -----------------------------------------------------------

@router.get(
    "/test-suites",
    dependencies=[Depends(require_scopes("api/nova/suite.read"))],
)
def list_test_suites() -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(SUITES_PK) & Key("sk").begins_with("SUITE#")
    )
    suites = resp.get("Items", [])
    for s in suites:
        s["usecaseCount"] = _member_count(s["id"])
    return {"testSuites": to_jsonable(suites)}


@router.post("/test-suites", status_code=status.HTTP_201_CREATED)
def create_test_suite(
    body: SuiteCreate,
    principal: Principal = Depends(require_scopes("api/nova/suite.write")),
) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    _validate_suite_fields(name, body.description)

    suite_id = str(uuid.uuid4())
    now = _now()
    item = {
        "pk": SUITES_PK,
        "sk": _suite_sk(suite_id),
        "id": suite_id,
        "name": name,
        "description": body.description,
        "tags": body.tags,
        "created_at": now,
        "created_by": principal.username,
    }
    get_table().put_item(Item=item)
    item["usecaseCount"] = 0
    logger.info("test suite %s created (%r)", suite_id, name)
    return to_jsonable(item)


@router.get(
    "/test-suites/{suite_id}",
    dependencies=[Depends(require_scopes("api/nova/suite.read"))],
)
def get_test_suite(suite_id: str) -> dict:
    item = _get_suite_item(suite_id)
    item["usecaseCount"] = _member_count(suite_id)
    return to_jsonable(item)


@router.put(
    "/test-suites/{suite_id}",
    dependencies=[Depends(require_scopes("api/nova/suite.write"))],
)
def update_test_suite(suite_id: str, body: SuiteUpdate) -> dict:
    _validate_suite_fields(body.name, body.description)

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "name" in updates:
        updates["name"] = updates["name"].strip()
    if not updates:
        return get_test_suite(suite_id)

    expr = ", ".join(f"#{k} = :{k}" for k in updates)
    try:
        get_table().update_item(
            Key={"pk": SUITES_PK, "sk": _suite_sk(suite_id)},
            UpdateExpression=f"SET {expr}",
            ExpressionAttributeNames={f"#{k}": k for k in updates},
            ExpressionAttributeValues={f":{k}": v for k, v in updates.items()},
            ConditionExpression="attribute_exists(pk)",  # 404 instead of silent upsert
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Test suite not found")
        raise
    return get_test_suite(suite_id)


@router.delete(
    "/test-suites/{suite_id}",
    dependencies=[Depends(require_scopes("api/nova/suite.write"))],
)
def delete_test_suite(suite_id: str) -> dict:
    table = get_table()
    _get_suite_item(suite_id)  # 404 if missing

    # Cascade: suite runs (each with its member executions + S3), membership
    # rows, then the suite itself.
    suite_execs = _suite_exec_items(suite_id)
    for se in suite_execs:
        _delete_suite_execution(suite_id, se["id"])
    for m in _member_items(suite_id):
        table.delete_item(Key={"pk": m["pk"], "sk": m["sk"]})
    table.delete_item(Key={"pk": SUITES_PK, "sk": _suite_sk(suite_id)})
    logger.info("test suite %s deleted (cascade: %d run(s))", suite_id, len(suite_execs))
    return {"status": "deleted", "id": suite_id}


# ---- membership -----------------------------------------------------------

@router.get(
    "/test-suites/{suite_id}/usecases",
    dependencies=[Depends(require_scopes("api/nova/suite.read"))],
)
def list_suite_usecases(suite_id: str) -> dict:
    _get_suite_item(suite_id)  # 404 if missing
    table = get_table()
    out = []
    for m in _member_items(suite_id):
        uc_id = m["usecase_id"]
        uc = table.get_item(Key={"pk": "USECASES", "sk": f"USECASE#{uc_id}"}).get("Item")
        out.append(
            {
                "usecaseId": uc_id,
                "sort": int(m.get("sort", 0)),
                "addedAt": m.get("added_at"),
                "name": uc.get("name") if uc else None,
                "description": uc.get("description", "") if uc else "",
                "active": uc.get("active", False) if uc else False,
                "missing": uc is None,  # use case was deleted after being added
            }
        )
    return {"usecases": to_jsonable(out)}


@router.post(
    "/test-suites/{suite_id}/usecases",
    dependencies=[Depends(require_scopes("api/nova/suite.write"))],
)
def add_usecases_to_suite(
    suite_id: str,
    body: AddUsecasesRequest,
    principal: Principal = Depends(require_scopes("api/nova/suite.write")),
) -> dict:
    _get_suite_item(suite_id)  # 404 if missing
    table = get_table()

    requested = [u for u in dict.fromkeys(body.usecaseIds) if u]  # de-dupe, keep order
    if not requested:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "usecaseIds is required")

    # Every requested use case must exist.
    missing = [
        u
        for u in requested
        if not table.get_item(Key={"pk": "USECASES", "sk": f"USECASE#{u}"}).get("Item")
    ]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown use case(s): {', '.join(missing)}",
        )

    existing = {m["usecase_id"] for m in _member_items(suite_id)}
    next_sort = 1 + max((int(m.get("sort", 0)) for m in _member_items(suite_id)), default=0)

    now = _now()
    added = []
    for uc_id in requested:
        if uc_id in existing:
            continue  # idempotent — already a member
        table.put_item(
            Item={
                "pk": _membership_pk(suite_id),
                "sk": f"USECASE#{uc_id}",
                "usecase_id": uc_id,
                "sort": next_sort,
                "added_at": now,
                "added_by": principal.username,
            }
        )
        added.append(uc_id)
        next_sort += 1

    logger.info("suite %s: added %d use case(s)", suite_id, len(added))
    return {"added": added, "usecaseCount": _member_count(suite_id)}


@router.delete(
    "/test-suites/{suite_id}/usecases/{usecase_id}",
    dependencies=[Depends(require_scopes("api/nova/suite.write"))],
)
def remove_usecase_from_suite(suite_id: str, usecase_id: str) -> dict:
    _get_suite_item(suite_id)  # 404 if missing
    get_table().delete_item(
        Key={"pk": _membership_pk(suite_id), "sk": f"USECASE#{usecase_id}"}
    )
    return {"status": "removed", "usecaseId": usecase_id}


# ---- suite execution (Phase 2) --------------------------------------------

class SuiteExecuteRequest(BaseModel):
    mode: str = "local"
    trigger: str | None = None
    capture: str | None = None  # run_now only


@router.post(
    "/test-suites/{suite_id}/execute",
    status_code=status.HTTP_201_CREATED,
)
def execute_test_suite(
    suite_id: str,
    body: SuiteExecuteRequest,
    principal: Principal = Depends(require_scopes("api/nova/suite.write")),
) -> dict:
    """Run a suite = the same "one endpoint, per-member execution" seam as §5.

    * mode `local`   — create the suite run + one PENDING use-case execution per
      member (stamped `suite_execution_id`); the CLI (`qa nova run-suite`) runs
      them and reports back.
    * mode `run_now` — same records, then launch **one Fargate task per member**
      (ecs.run_task, reusing the single-use-case launcher) so they run in
      parallel headless via the task role.
    """
    suite = _get_suite_item(suite_id)
    if body.mode not in ("local", "run_now"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"mode '{body.mode}' is not supported (use 'local' or 'run_now')",
        )
    capture = ""
    if body.mode == "run_now":
        s = get_settings()
        if not s.ecs_enabled:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "run_now is not configured on this server (no ECS cluster/task definition)",
            )
        capture = (body.capture or s.runner_capture).lower()
        if capture not in VALID_CAPTURE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"capture must be one of {sorted(VALID_CAPTURE)}")

    table = get_table()
    runnable: list[tuple[str, str]] = []
    skipped: list[str] = []
    for m in _member_items(suite_id):
        uc_id = m["usecase_id"]
        uc = table.get_item(Key={"pk": "USECASES", "sk": f"USECASE#{uc_id}"}).get("Item")
        if uc:
            runnable.append((uc_id, uc.get("name") or "(untitled)"))
        else:
            skipped.append(uc_id)  # deleted after being added — can't run
    if not runnable:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Suite has no runnable use cases")

    se_id = str(uuid.uuid4())
    now = _now_z()
    roster = []
    for uc_id, uc_name in runnable:
        eid = str(uuid.uuid4())
        item = {
            "pk": f"USECASE_EXECUTION#{uc_id}",
            "sk": f"EXECUTION#{eid}",
            "executionId": eid,
            "usecaseId": uc_id,
            "status": "pending",
            "mode": body.mode,
            "trigger": body.trigger or "suite",
            "createdBy": principal.username,
            "createdAt": now,
            "stopRequested": False,
            "suite_execution_id": se_id,  # -> suite-execution-index GSI
            "suite_id": suite_id,
        }
        if capture:
            item["capture"] = capture
        table.put_item(Item=item)
        roster.append({"executionId": eid, "usecaseId": uc_id, "usecaseName": uc_name})

    table.put_item(
        Item={
            "pk": _suite_exec_pk(suite_id),
            "sk": f"EXECUTION#{se_id}",
            "id": se_id,
            "suite_id": suite_id,
            "suite_name": suite.get("name", ""),
            "mode": body.mode,
            "trigger": body.trigger or "suite",
            "triggeredBy": principal.username,
            "createdAt": now,
            "totalUsecases": len(runnable),
            "members": roster,  # roster snapshot (name + ids) for the history view
        }
    )

    launched = 0
    if body.mode == "run_now":
        # One Fargate task per member (they run in parallel). A launch failure
        # marks just that member failed — the rest still go.
        for r in roster:
            try:
                task_arn = _launch_ecs_task(r["usecaseId"], r["executionId"], capture)
                table.update_item(
                    Key={"pk": f"USECASE_EXECUTION#{r['usecaseId']}", "sk": f"EXECUTION#{r['executionId']}"},
                    UpdateExpression="SET taskArn = :ta",
                    ExpressionAttributeValues={":ta": task_arn},
                )
                launched += 1
            except HTTPException as e:
                logger.error("suite %s: launch failed for member %s: %s", suite_id, r["usecaseId"], e.detail)
                table.update_item(
                    Key={"pk": f"USECASE_EXECUTION#{r['usecaseId']}", "sk": f"EXECUTION#{r['executionId']}"},
                    UpdateExpression="SET #s = :s, errorMessage = :em, endedAt = :e",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "failed", ":em": "Failed to launch ECS task", ":e": _now_z()},
                )

    logger.info("suite %s executed -> run %s (mode=%s, %d member(s), %d skipped, %d launched)",
                suite_id, se_id, body.mode, len(runnable), len(skipped), launched)
    out = {"suiteExecutionId": se_id, "executions": roster, "total": len(runnable), "skipped": skipped, "mode": body.mode}
    if body.mode == "run_now":
        out["launched"] = launched
    return out


@router.get(
    "/test-suites/{suite_id}/executions",
    dependencies=[Depends(require_scopes("api/nova/suite.read"))],
)
def list_suite_executions(suite_id: str) -> dict:
    _get_suite_item(suite_id)  # 404 if missing
    out = []
    for se in _suite_exec_items(suite_id):
        overall, counts = _rollup(_member_executions(se["id"]))
        out.append({
            "suiteExecutionId": se["id"],
            "status": overall,
            "counts": counts,
            "totalUsecases": int(se.get("totalUsecases", counts["total"])),
            "mode": se.get("mode"),
            "trigger": se.get("trigger"),
            "triggeredBy": se.get("triggeredBy"),
            "createdAt": se.get("createdAt"),
        })
    out.sort(key=lambda e: e.get("createdAt", ""), reverse=True)
    return {"executions": to_jsonable(out)}


@router.get(
    "/test-suites/{suite_id}/executions/{se_id}",
    dependencies=[Depends(require_scopes("api/nova/suite.read"))],
)
def get_suite_execution(suite_id: str, se_id: str) -> dict:
    se = _get_suite_exec_or_404(suite_id, se_id)
    live = _member_executions(se_id)
    overall, counts = _rollup(live)

    roster = se.get("members", [])
    order = {r["executionId"]: i for i, r in enumerate(roster)}
    names = {r["executionId"]: r.get("usecaseName") for r in roster}
    members = []
    for m in live:
        row = {k: m[k] for k in MEMBER_FIELDS if k in m}
        row["usecaseName"] = names.get(m.get("executionId"))
        members.append(row)
    members.sort(key=lambda x: order.get(x.get("executionId"), 1_000_000))

    return to_jsonable({
        "suiteExecutionId": se_id,
        "suiteId": suite_id,
        "suiteName": se.get("suite_name"),
        "status": overall,
        "counts": counts,
        "totalUsecases": int(se.get("totalUsecases", counts["total"])),
        "mode": se.get("mode"),
        "trigger": se.get("trigger"),
        "triggeredBy": se.get("triggeredBy"),
        "createdAt": se.get("createdAt"),
        "members": members,
    })


@router.delete(
    "/test-suites/{suite_id}/executions/{se_id}",
    dependencies=[Depends(require_scopes("api/nova/suite.write"))],
)
def delete_suite_execution(suite_id: str, se_id: str) -> dict:
    _get_suite_exec_or_404(suite_id, se_id)
    members = _member_executions(se_id)
    _delete_suite_execution(suite_id, se_id)
    logger.info("suite run %s deleted (%d member execution(s))", se_id, len(members))
    return {"status": "deleted", "suiteExecutionId": se_id, "membersDeleted": len(members)}


@router.post(
    "/test-suites/{suite_id}/executions/{se_id}/stop",
    dependencies=[Depends(require_scopes("api/nova/suite.write"))],
)
def stop_suite_execution(suite_id: str, se_id: str) -> dict:
    """Cooperative stop: flag every not-yet-finished member. The local runner
    checks the flag at each step boundary and halts itself."""
    _get_suite_exec_or_404(suite_id, se_id)
    table = get_table()
    now = _now_z()
    stopped = 0
    for m in _member_executions(se_id):
        if m.get("status") in EXEC_TERMINAL:
            continue
        uc, eid = m["usecaseId"], m["executionId"]
        table.update_item(
            Key={"pk": f"USECASE_EXECUTION#{uc}", "sk": f"EXECUTION#{eid}"},
            UpdateExpression="SET stopRequested = :t, #s = :st, endedAt = :e",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":t": True, ":st": "stopped", ":e": now},
        )
        stopped += 1
    logger.info("suite run %s stop requested (%d member(s))", se_id, stopped)
    return {"status": "stop requested", "stopRequested": stopped}
