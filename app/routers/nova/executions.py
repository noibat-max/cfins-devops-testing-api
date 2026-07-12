"""Execution engine — §5 (local-tracked path).

Records a test run and its artifacts. `POST /usecase/{id}/execute` with mode
`local` creates an execution record ONLY (no ECS); the CLI runs the browser and
reports back over REST (PAT or JWT auth), uploading artifacts via presigned S3
URLs. Both the CLI (write) and the UI (read) are served. Read-live — we don't
snapshot usecase/step definitions; EXECUTION_STEP items are per-run *results*.

Data model (single table):
  Execution   pk="USECASE_EXECUTION#<uc>"  sk="EXECUTION#<eid>"
  Step result pk="EXECUTION#<eid>"         sk="EXECUTION_STEP#<sid>"   (upserted)
  Artifact    pk="EXECUTION#<eid>"         sk="ARTIFACT#<aid>"         (+ S3 object)

Deferred (remote / rich-capture / PROD): run_now/queued/scheduled modes, live
view, video, downloads, events, step trace.
"""
from __future__ import annotations

import datetime
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...aws import get_s3_client, get_table
from ...config import get_settings
from ...security import Principal, require_scopes
from ...serialization import to_jsonable

router = APIRouter(tags=["executions"])

# --- status vocabularies ---
EXEC_STATUSES = {"pending", "executing", "completed", "failed", "stopped"}
EXEC_TERMINAL = {"completed", "failed", "stopped"}
STEP_STATUSES = {"pending", "executing", "passed", "failed", "skipped"}
STEP_DONE = {"passed", "failed", "skipped"}

PRESIGN_TTL = 900  # 15 min — artifacts are consumed right after minting

EXEC_FIELDS = (
    "executionId", "usecaseId", "status", "mode", "trigger",
    "createdBy", "createdAt", "startedAt", "endedAt", "errorMessage",
    "stopRequested",
)
STEP_FIELDS = (
    "stepId", "sort", "status", "startedAt", "endedAt",
    "errorMessage", "result", "updatedAt",
)
ARTIFACT_FIELDS = (
    "artifactId", "artifactType", "filename", "contentType", "stepId",
    "status", "sizeBytes", "createdAt",
)


# ------------------------------------------------------------------ helpers ---
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project(item: dict, keys) -> dict:
    return to_jsonable({k: item[k] for k in keys if k in item})


def _query_all(**kwargs) -> list[dict]:
    """Query, following pagination so we never silently truncate."""
    table = get_table()
    resp = table.query(**kwargs)
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items += resp.get("Items", [])
    return items


def _get_usecase_or_404(usecase_id: str) -> dict:
    item = get_table().get_item(
        Key={"pk": "USECASES", "sk": f"USECASE#{usecase_id}"}
    ).get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Use case not found")
    return item


def _get_execution_or_404(usecase_id: str, eid: str) -> dict:
    item = get_table().get_item(
        Key={"pk": f"USECASE_EXECUTION#{usecase_id}", "sk": f"EXECUTION#{eid}"}
    ).get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Execution not found")
    return item


def _presign(client_method: str, key: str) -> str:
    return get_s3_client().generate_presigned_url(
        client_method,
        Params={"Bucket": get_settings().artifacts_bucket, "Key": key},
        ExpiresIn=PRESIGN_TTL,
    )


# --------------------------------------------------------------- lifecycle ---
class ExecuteRequest(BaseModel):
    mode: str = "local"
    trigger: str | None = None


@router.post("/usecase/{usecase_id}/execute", status_code=status.HTTP_201_CREATED)
def execute(
    usecase_id: str,
    body: ExecuteRequest,
    principal: Principal = Depends(require_scopes("api/nova/usecases.execute")),
) -> dict:
    """Create an execution record. mode `local` = record-only (CLI runs it).

    `run_now`/`queued`/`scheduled` (remote ECS) are deferred — 400 for now.
    """
    _get_usecase_or_404(usecase_id)
    if body.mode != "local":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"mode '{body.mode}' is not yet supported (only 'local')",
        )

    eid = str(uuid.uuid4())
    item = {
        "pk": f"USECASE_EXECUTION#{usecase_id}",
        "sk": f"EXECUTION#{eid}",
        "executionId": eid,
        "usecaseId": usecase_id,
        "status": "pending",
        "mode": "local",
        "trigger": body.trigger or "cli",
        "createdBy": principal.username,
        "createdAt": _now(),
        "stopRequested": False,
    }
    get_table().put_item(Item=item)
    return {"executionId": eid, "status": "pending", "mode": "local"}


@router.get("/usecase/{usecase_id}/executions")
def list_executions(
    usecase_id: str,
    _: Principal = Depends(require_scopes("api/nova/executions.read")),
) -> dict:
    items = _query_all(
        KeyConditionExpression=Key("pk").eq(f"USECASE_EXECUTION#{usecase_id}")
    )
    execs = [_project(i, EXEC_FIELDS) for i in items]
    execs.sort(key=lambda e: e.get("createdAt", ""), reverse=True)
    return {"executions": execs}


@router.get("/usecase/{usecase_id}/executions/{eid}")
def get_execution(
    usecase_id: str,
    eid: str,
    _: Principal = Depends(require_scopes("api/nova/executions.read")),
) -> dict:
    return _project(_get_execution_or_404(usecase_id, eid), EXEC_FIELDS)


@router.delete("/usecase/{usecase_id}/executions/{eid}")
def delete_execution(
    usecase_id: str,
    eid: str,
    _: Principal = Depends(require_scopes("api/nova/executions.write")),
) -> dict:
    """Cascade: delete step + artifact items AND the artifacts' S3 objects."""
    _get_execution_or_404(usecase_id, eid)
    table = get_table()
    children = _query_all(KeyConditionExpression=Key("pk").eq(f"EXECUTION#{eid}"))

    s3 = get_s3_client()
    bucket = get_settings().artifacts_bucket
    artifacts_deleted = 0
    for it in children:
        if it["sk"].startswith("ARTIFACT#") and it.get("s3Key"):
            try:
                s3.delete_object(Bucket=bucket, Key=it["s3Key"])
                artifacts_deleted += 1
            except ClientError:
                pass  # best-effort; still remove the record
        table.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})

    table.delete_item(
        Key={"pk": f"USECASE_EXECUTION#{usecase_id}", "sk": f"EXECUTION#{eid}"}
    )
    return {"status": "deleted", "executionId": eid, "artifactsDeleted": artifacts_deleted}


@router.post("/usecase/{usecase_id}/executions/{eid}/stop")
def stop_execution(
    usecase_id: str,
    eid: str,
    _: Principal = Depends(require_scopes("api/nova/executions.write")),
) -> dict:
    """Cooperative stop: set stopRequested (the CLI honours it between steps).

    A local browser can't be killed remotely; if still running we also mark the
    record stopped. True remote kill is the ECS path (deferred).
    """
    item = _get_execution_or_404(usecase_id, eid)
    set_parts = ["stopRequested = :sr"]
    names: dict[str, str] = {}
    values: dict = {":sr": True}
    if item.get("status") in {"pending", "executing"}:
        set_parts += ["#s = :st", "endedAt = :e"]
        names["#s"] = "status"
        values[":st"] = "stopped"
        values[":e"] = _now()
    _update(f"USECASE_EXECUTION#{usecase_id}", f"EXECUTION#{eid}", set_parts, names, values)
    return {"status": "stop requested", "executionId": eid}


# --------------------------------------------------------- status callbacks ---
class ExecStatusUpdate(BaseModel):
    status: str
    errorMessage: str | None = None


@router.patch("/usecase/{usecase_id}/executions/{eid}/status")
def update_execution_status(
    usecase_id: str,
    eid: str,
    body: ExecStatusUpdate,
    _: Principal = Depends(require_scopes("api/nova/executions.write")),
) -> dict:
    _get_execution_or_404(usecase_id, eid)
    if body.status not in EXEC_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status '{body.status}'")

    set_parts = ["#s = :s"]
    names = {"#s": "status"}
    values: dict = {":s": body.status}
    if body.status == "executing":
        set_parts.append("startedAt = if_not_exists(startedAt, :t)")
        values[":t"] = _now()
    if body.status in EXEC_TERMINAL:
        set_parts.append("endedAt = :e")
        values[":e"] = _now()
    if body.errorMessage is not None:
        set_parts.append("errorMessage = :em")
        values[":em"] = body.errorMessage

    _update(f"USECASE_EXECUTION#{usecase_id}", f"EXECUTION#{eid}", set_parts, names, values)
    return {"executionId": eid, "status": body.status}


class StepStatusUpdate(BaseModel):
    status: str
    sort: int | None = None
    errorMessage: str | None = None
    result: str | None = None


@router.patch("/usecase/{usecase_id}/executions/{eid}/steps/{step_id}/status")
def update_step_status(
    usecase_id: str,
    eid: str,
    step_id: str,
    body: StepStatusUpdate,
    _: Principal = Depends(require_scopes("api/nova/executions.write")),
) -> dict:
    """Upsert a per-step result (no pre-created rows — read-live)."""
    _get_execution_or_404(usecase_id, eid)
    if body.status not in STEP_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid status '{body.status}'")

    set_parts = ["#s = :s", "stepId = :sid", "updatedAt = :u"]
    names = {"#s": "status"}
    values: dict = {":s": body.status, ":sid": step_id, ":u": _now()}
    if body.sort is not None:
        set_parts.append("#sort = :srt")
        names["#sort"] = "sort"
        values[":srt"] = body.sort
    if body.status == "executing":
        set_parts.append("startedAt = if_not_exists(startedAt, :t)")
        values[":t"] = _now()
    if body.status in STEP_DONE:
        set_parts.append("endedAt = :e")
        values[":e"] = _now()
    if body.errorMessage is not None:
        set_parts.append("errorMessage = :em")
        values[":em"] = body.errorMessage
    if body.result is not None:
        set_parts.append("#r = :res")
        names["#r"] = "result"
        values[":res"] = body.result

    # upsert (no existence guard — the first callback creates the row)
    _update(f"EXECUTION#{eid}", f"EXECUTION_STEP#{step_id}", set_parts, names, values)
    return {"stepId": step_id, "status": body.status}


# ------------------------------------------------------------------- reads ---
@router.get("/usecase/{usecase_id}/executions/{eid}/steps")
def list_execution_steps(
    usecase_id: str,
    eid: str,
    _: Principal = Depends(require_scopes("api/nova/executions.read")),
) -> dict:
    _get_execution_or_404(usecase_id, eid)
    items = _query_all(
        KeyConditionExpression=Key("pk").eq(f"EXECUTION#{eid}")
        & Key("sk").begins_with("EXECUTION_STEP#")
    )
    steps = [_project(i, STEP_FIELDS) for i in items]
    steps.sort(key=lambda s: (s.get("sort") is None, s.get("sort", 0)))
    return {"steps": steps}


@router.get("/usecase/{usecase_id}/executions/{eid}/steps/{step_id}")
def get_execution_step(
    usecase_id: str,
    eid: str,
    step_id: str,
    _: Principal = Depends(require_scopes("api/nova/executions.read")),
) -> dict:
    item = get_table().get_item(
        Key={"pk": f"EXECUTION#{eid}", "sk": f"EXECUTION_STEP#{step_id}"}
    ).get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Execution step not found")
    return _project(item, STEP_FIELDS)


# --------------------------------------------------------------- artifacts ---
class ArtifactCreate(BaseModel):
    filename: str
    contentType: str = "application/octet-stream"
    artifactType: str = "file"
    sizeBytes: int | None = None


def _mint_artifact(usecase_id: str, eid: str, body: ArtifactCreate, step_id: str | None) -> dict:
    _get_execution_or_404(usecase_id, eid)
    fname = body.filename.strip().lstrip("/").replace("..", "")
    if not fname:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "filename is required")

    aid = str(uuid.uuid4())
    key = f"executions/{eid}/{aid}/{fname}"
    item = {
        "pk": f"EXECUTION#{eid}",
        "sk": f"ARTIFACT#{aid}",
        "artifactId": aid,
        "artifactType": body.artifactType,
        "filename": fname,
        "contentType": body.contentType,
        "bucket": get_settings().artifacts_bucket,
        "s3Key": key,
        "status": "pending",
        "createdAt": _now(),
    }
    if step_id:
        item["stepId"] = step_id
    if body.sizeBytes is not None:
        item["sizeBytes"] = body.sizeBytes
    get_table().put_item(Item=item)

    return {"artifactId": aid, "uploadUrl": _presign("put_object", key), "expiresIn": PRESIGN_TTL}


@router.post(
    "/usecase/{usecase_id}/executions/{eid}/artifacts",
    status_code=status.HTTP_201_CREATED,
)
def create_execution_artifact(
    usecase_id: str,
    eid: str,
    body: ArtifactCreate,
    _: Principal = Depends(require_scopes("api/nova/executions.write")),
) -> dict:
    return _mint_artifact(usecase_id, eid, body, step_id=None)


@router.post(
    "/usecase/{usecase_id}/executions/{eid}/steps/{step_id}/artifacts",
    status_code=status.HTTP_201_CREATED,
)
def create_step_artifact(
    usecase_id: str,
    eid: str,
    step_id: str,
    body: ArtifactCreate,
    _: Principal = Depends(require_scopes("api/nova/executions.write")),
) -> dict:
    return _mint_artifact(usecase_id, eid, body, step_id=step_id)


class ArtifactConfirm(BaseModel):
    status: str = "uploaded"
    sizeBytes: int | None = None


@router.patch("/usecase/{usecase_id}/executions/{eid}/artifacts/{aid}")
def confirm_artifact(
    usecase_id: str,
    eid: str,
    aid: str,
    body: ArtifactConfirm,
    _: Principal = Depends(require_scopes("api/nova/executions.write")),
) -> dict:
    key = {"pk": f"EXECUTION#{eid}", "sk": f"ARTIFACT#{aid}"}
    if not get_table().get_item(Key=key).get("Item"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")

    set_parts = ["#s = :s"]
    names = {"#s": "status"}
    values: dict = {":s": body.status or "uploaded"}
    if body.sizeBytes is not None:
        set_parts.append("sizeBytes = :sz")
        values[":sz"] = body.sizeBytes
    _update(f"EXECUTION#{eid}", f"ARTIFACT#{aid}", set_parts, names, values)
    return {"artifactId": aid, "status": values[":s"]}


@router.get("/usecase/{usecase_id}/executions/{eid}/artifacts")
def list_artifacts(
    usecase_id: str,
    eid: str,
    _: Principal = Depends(require_scopes("api/nova/executions.read")),
) -> dict:
    _get_execution_or_404(usecase_id, eid)
    items = _query_all(
        KeyConditionExpression=Key("pk").eq(f"EXECUTION#{eid}")
        & Key("sk").begins_with("ARTIFACT#")
    )
    out = []
    for it in items:
        d = _project(it, ARTIFACT_FIELDS)
        # Mint a GET URL for finished uploads so the UI can render them directly.
        if it.get("status") == "uploaded" and it.get("s3Key"):
            d["url"] = _presign("get_object", it["s3Key"])
        out.append(d)
    out.sort(key=lambda a: a.get("createdAt", ""))
    return {"artifacts": out}


# -------------------------------------------------------------- update util ---
def _update(pk: str, sk: str, set_parts: list[str], names: dict, values: dict) -> None:
    kwargs: dict = {
        "Key": {"pk": pk, "sk": sk},
        "UpdateExpression": "SET " + ", ".join(set_parts),
        "ExpressionAttributeValues": values,
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    get_table().update_item(**kwargs)
