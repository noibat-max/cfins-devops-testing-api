"""Use cases — core CRUD.

Ports sample-qa-studio's usecase list/create/get/update/delete. Single-table:
  * use case     pk="USECASES"        sk="USECASE#<id>"
  * created-by   pk="USECASE#<id>"    sk="CREATED_BY"
  * steps        pk="USECASE#<id>"    sk="STEP#..."          (deleted on cascade)
  * executions   pk="USECASE_EXECUTION#<id>" sk="EXECUTION#..." (cascade)

Stored attribute names match the sample exactly (snake_case) so the future
worker/execution engine reads the same shape. Mobile/Device-Farm fields are
dropped (out of scope) — test_platform is always "web".
"""
from __future__ import annotations

import datetime
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..aws import get_table
from ..security import Principal, require_scopes
from ..serialization import to_jsonable

router = APIRouter(tags=["usecases"])

DEFAULT_REGION = "us-east-1"
DEFAULT_MODEL = "nova-act-v1.0"
EXPORT_VERSION = "1.0"

# Clean (no id/timestamp) usecase fields for export.
_EXPORT_USECASE_FIELDS = (
    "name",
    "description",
    "starting_url",
    "active",
    "executing_region",
    "tags",
    "model_id",
    "test_platform",
)
# Fields copied from source when cloning (name comes from the request).
_CLONE_COPY_FIELDS = (
    "description",
    "starting_url",
    "active",
    "executing_region",
    "tags",
    "model_id",
    "enable_cache",
    "test_platform",
)
# Optional step fields carried through export/clone/import when truthy.
_STEP_CARRY_FIELDS = (
    "secret_key",
    "capture_variable",
    "validation_type",
    "validation_operator",
    "validation_value",
    "assertion_variable",
    "value_type",
    "value_source",
)

# API field -> stored attribute (enableCache is the sample's camelCase alias).
_UPDATABLE = {
    "name": "name",
    "description": "description",
    "starting_url": "starting_url",
    "active": "active",
    "executing_region": "executing_region",
    "model_id": "model_id",
    "tags": "tags",
    "enableCache": "enable_cache",
}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UsecaseCreate(BaseModel):
    name: str = ""
    description: str = ""
    starting_url: str = ""
    active: bool = False
    tags: list[str] = []
    executing_region: str | None = None
    model_id: str | None = None
    enableCache: bool = False


class UsecaseUpdate(BaseModel):
    # All optional — PATCH updates only the fields provided.
    name: str | None = None
    description: str | None = None
    starting_url: str | None = None
    active: bool | None = None
    executing_region: str | None = None
    model_id: str | None = None
    tags: list[str] | None = None
    enableCache: bool | None = None


class CloneRequest(BaseModel):
    name: str


class ImportRequest(BaseModel):
    # Accepts the export envelope; extra keys (variables/secrets/hooks) ignored
    # until §3 config is ported.
    exportVersion: str | None = None
    usecase: dict = {}
    steps: list[dict] = []


def _get_usecase_item(usecase_id: str) -> dict:
    resp = get_table().get_item(Key={"pk": "USECASES", "sk": f"USECASE#{usecase_id}"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Use case not found")
    return item


def _get_sorted_steps(usecase_id: str) -> list[dict]:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(f"USECASE#{usecase_id}")
        & Key("sk").begins_with("STEP#")
    )
    steps = resp.get("Items", [])
    steps.sort(key=lambda s: s.get("sort", 0))
    return steps


def _write_step(usecase_id: str, sort, source: dict, now: str) -> None:
    """Create a fresh step (new id) from a source/import dict."""
    step_id = str(uuid.uuid4())
    step = {
        "pk": f"USECASE#{usecase_id}",
        "sk": f"STEP#{step_id}",
        "id": step_id,
        "sort": sort,
        "instruction": source.get("instruction", ""),
        "step_type": source.get("step_type", ""),
        "created_at": now,
    }
    for field in _STEP_CARRY_FIELDS:
        if source.get(field):
            step[field] = source[field]
    get_table().put_item(Item=step)


def _write_created_by(usecase_id: str, principal: Principal, now: str) -> None:
    get_table().put_item(
        Item={
            "pk": f"USECASE#{usecase_id}",
            "sk": "CREATED_BY",
            "email": principal.email,
            "sub": principal.username,
            "created_at": now,
        }
    )


@router.get("/usecases", dependencies=[Depends(require_scopes("api/usecases.read"))])
def list_usecases() -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq("USECASES") & Key("sk").begins_with("USECASE#")
    )
    return {"usecases": resp.get("Items", [])}


@router.post("/usecase", status_code=status.HTTP_201_CREATED)
def create_usecase(
    body: UsecaseCreate,
    principal: Principal = Depends(require_scopes("api/usecases.write")),
) -> dict:
    table = get_table()
    usecase_id = str(uuid.uuid4())
    now = _now()

    item = {
        "pk": "USECASES",
        "sk": f"USECASE#{usecase_id}",
        "id": usecase_id,
        "name": body.name,
        "description": body.description,
        "starting_url": body.starting_url,
        "active": body.active,
        "tags": body.tags,
        "created_at": now,
        "executing_region": body.executing_region or DEFAULT_REGION,
        "model_id": body.model_id or DEFAULT_MODEL,
        "enable_cache": body.enableCache,
        "test_platform": "web",
    }
    table.put_item(Item=item)

    # Who created it — from the token identity (local or Cognito).
    table.put_item(
        Item={
            "pk": f"USECASE#{usecase_id}",
            "sk": "CREATED_BY",
            "email": principal.email,
            "sub": principal.username,
        }
    )

    item["enableCache"] = item["enable_cache"]
    return item


@router.get(
    "/usecase/{usecase_id}",
    dependencies=[Depends(require_scopes("api/usecases.read"))],
)
def get_usecase(usecase_id: str) -> dict:
    resp = get_table().get_item(Key={"pk": "USECASES", "sk": f"USECASE#{usecase_id}"})
    item = resp.get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Use case not found")
    item["enableCache"] = item.get("enable_cache", False)
    return item


@router.patch(
    "/usecase/{usecase_id}",
    dependencies=[Depends(require_scopes("api/usecases.write"))],
)
def update_usecase(usecase_id: str, body: UsecaseUpdate) -> dict:
    provided = body.model_dump(exclude_unset=True)

    # Empty/whitespace region falls back to the default (sample behavior).
    if "executing_region" in provided and not (provided["executing_region"] or "").strip():
        provided["executing_region"] = DEFAULT_REGION

    if not provided:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    set_parts: list[str] = []
    names: dict[str, str] = {}
    values: dict = {}
    for field, value in provided.items():
        attr = _UPDATABLE[field]
        if attr == "name":  # reserved word
            names["#name"] = "name"
            set_parts.append("#name = :name")
            values[":name"] = value
        else:
            set_parts.append(f"{attr} = :{attr}")
            values[f":{attr}"] = value

    kwargs = {
        "Key": {"pk": "USECASES", "sk": f"USECASE#{usecase_id}"},
        "UpdateExpression": "SET " + ", ".join(set_parts),
        "ExpressionAttributeValues": values,
        "ConditionExpression": "attribute_exists(pk)",  # 404 instead of silent upsert
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names

    try:
        get_table().update_item(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Use case not found")
        raise

    return {"status": "usecase updated", "usecaseId": usecase_id}


@router.delete(
    "/usecase/{usecase_id}",
    dependencies=[Depends(require_scopes("api/usecases.write"))],
)
def delete_usecase(usecase_id: str) -> dict:
    table = get_table()

    # Use-case metadata + created-by record.
    table.delete_item(Key={"pk": "USECASES", "sk": f"USECASE#{usecase_id}"})
    table.delete_item(Key={"pk": f"USECASE#{usecase_id}", "sk": "CREATED_BY"})

    # Cascade: steps.
    steps = table.query(
        KeyConditionExpression=Key("pk").eq(f"USECASE#{usecase_id}")
        & Key("sk").begins_with("STEP#")
    )
    for it in steps.get("Items", []):
        table.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})

    # Cascade: executions and their execution-steps.
    execs = table.query(
        KeyConditionExpression=Key("pk").eq(f"USECASE_EXECUTION#{usecase_id}")
        & Key("sk").begins_with("EXECUTION#")
    )
    for it in execs.get("Items", []):
        table.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
        exec_steps = table.query(
            KeyConditionExpression=Key("pk").eq(f"EXECUTION#{it['pk']}")
            & Key("sk").begins_with("EXECUTION_STEP#")
        )
        for step in exec_steps.get("Items", []):
            table.delete_item(Key={"pk": step["pk"], "sk": step["sk"]})

    return {"status": "usecase deleted", "usecaseId": usecase_id}


@router.get(
    "/usecase/{usecase_id}/export",
    dependencies=[Depends(require_scopes("api/usecases.read"))],
)
def export_usecase(usecase_id: str) -> dict:
    usecase = _get_usecase_item(usecase_id)

    clean_usecase = {f: usecase.get(f, "") for f in _EXPORT_USECASE_FIELDS}
    clean_usecase["active"] = usecase.get("active", False)
    clean_usecase["tags"] = list(usecase.get("tags", []) or [])

    steps = []
    for s in _get_sorted_steps(usecase_id):
        step = {
            "sort": s.get("sort", 0),
            "instruction": s.get("instruction", ""),
            "step_type": s.get("step_type", ""),
        }
        for field in _STEP_CARRY_FIELDS:
            if s.get(field):
                step[field] = s[field]
        steps.append(step)

    export = {
        "exportVersion": EXPORT_VERSION,
        "usecase": clean_usecase,
        "steps": steps,
        # Empty until §3 (variables/secrets) is ported — keeps the envelope stable.
        "variables": [],
        "secrets": [],
    }
    return to_jsonable(export)


@router.post("/usecase/{usecase_id}/clone", status_code=status.HTTP_201_CREATED)
def clone_usecase(
    usecase_id: str,
    body: CloneRequest,
    principal: Principal = Depends(require_scopes("api/usecases.write")),
) -> dict:
    if not body.name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required")

    source = _get_usecase_item(usecase_id)
    new_id = str(uuid.uuid4())
    now = _now()

    new_usecase = {
        "pk": "USECASES",
        "sk": f"USECASE#{new_id}",
        "id": new_id,
        "name": body.name,
        "created_at": now,
    }
    for field in _CLONE_COPY_FIELDS:
        if field in source:
            new_usecase[field] = source[field]
    new_usecase.setdefault("test_platform", "web")

    get_table().put_item(Item=new_usecase)
    _write_created_by(new_id, principal, now)

    for s in _get_sorted_steps(usecase_id):
        _write_step(new_id, s.get("sort", 0), s, now)

    return {"success": True, "usecaseId": new_id, "message": "Usecase cloned"}


@router.post("/import", status_code=status.HTTP_201_CREATED)
def import_usecase(
    body: ImportRequest,
    principal: Principal = Depends(require_scopes("api/usecases.write")),
) -> dict:
    if body.exportVersion != EXPORT_VERSION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported export version")

    data = body.usecase or {}
    new_id = str(uuid.uuid4())
    now = _now()

    tags = list(data.get("tags", []) or [])
    if "imported" not in tags:
        tags.append("imported")

    new_usecase = {
        "pk": "USECASES",
        "sk": f"USECASE#{new_id}",
        "id": new_id,
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "starting_url": data.get("starting_url", ""),
        "active": data.get("active", False),
        "executing_region": data.get("executing_region", "") or DEFAULT_REGION,
        "model_id": data.get("model_id", "") or DEFAULT_MODEL,
        "tags": tags,
        "test_platform": data.get("test_platform", "web") or "web",
        "created_at": now,
    }
    get_table().put_item(Item=new_usecase)
    _write_created_by(new_id, principal, now)

    # Re-sequence steps 1..N on import (sample behavior).
    for i, step in enumerate(body.steps, start=1):
        _write_step(new_id, i, step, now)

    return {"success": True, "usecaseId": new_id, "message": "Usecase imported"}
