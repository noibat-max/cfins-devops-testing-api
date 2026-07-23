"""Templates (§7) — reusable, parameterized step libraries.

A template has the same shape as a use case's authoring side (steps + variables)
but is a *definition to reuse*, not something you run. Single-table:
  Template       pk="TEMPLATES"       sk="TEMPLATE#<id>"
  Template step  pk="TEMPLATE#<id>"   sk="STEP#<id>"           (same fields as use-case steps)
  Template vars  pk="TEMPLATE#<id>"   sk="TEMPLATE_VARIABLES"

Phase 1: CRUD + steps + variables + **apply** (→ a new use case) + **import**
(→ append into an existing use case). Deferred (phase 2): subscriptions and
drift/update-tracking.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...aws import get_client, get_table
from ...security import Principal, require_scopes
from ...serialization import to_jsonable
from .config import read_variables, write_variables
from .steps import ReorderRequest, StepCreate, StepUpdate, _OPTIONAL_STEP_FIELDS

logger = logging.getLogger("cfins.qawb.templates")

router = APIRouter(tags=["templates"])

DEFAULT_REGION = "us-east-1"
DEFAULT_MODEL = "nova-act-v1.0"

READ = require_scopes("api/qawb/templates.read")
WRITE = require_scopes("api/qawb/templates.write")
# apply/import both read a template AND write a use case → require both scopes.
APPLY = require_scopes("api/qawb/templates.read", "api/qawb/usecases.write")

_TEMPLATE_FIELDS = ("id", "name", "description", "created_at", "created_by", "version")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Content fields that define a step's behaviour — hashed so we can detect when a
# template step (or a use-case step derived from it) has changed since sync.
_HASH_FIELDS = (
    "instruction", "step_type", "secret_key", "validation_type", "validation_operator",
    "validation_value", "validation_tolerance", "capture_variable", "assertion_variable",
    "value_type", "enable_advanced_click_types", "value_source",
)


def _step_content_hash(step: dict) -> str:
    payload = json.dumps({f: str(step.get(f, "")) for f in _HASH_FIELDS}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _bump_version(tid: str) -> None:
    """Increment a template's version — called on any step mutation. Used as the
    cheap 'has anything changed?' signal for use cases derived from it."""
    get_table().update_item(
        Key={"pk": "TEMPLATES", "sk": f"TEMPLATE#{tid}"},
        UpdateExpression="ADD version :one",
        ExpressionAttributeValues={":one": 1},
    )


def _public(item: dict) -> dict:
    return to_jsonable({k: item[k] for k in _TEMPLATE_FIELDS if k in item})


def _get_template_or_404(tid: str) -> dict:
    item = get_table().get_item(Key={"pk": "TEMPLATES", "sk": f"TEMPLATE#{tid}"}).get("Item")
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return item


def _template_steps(tid: str) -> list[dict]:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(f"TEMPLATE#{tid}") & Key("sk").begins_with("STEP#")
    )
    return sorted(resp.get("Items", []), key=lambda s: s.get("sort", 0))


def _build_step_item(pk: str, body: StepCreate) -> dict:
    sid = str(uuid.uuid4())
    return {
        "pk": pk, "sk": f"STEP#{sid}", "id": sid,
        "sort": body.sort, "instruction": body.instruction, "step_type": body.step_type,
        "secret_key": body.secret_key, "validation_type": body.validation_type,
        "validation_operator": body.validation_operator, "validation_value": body.validation_value,
        "validation_tolerance": body.validation_tolerance, "capture_variable": body.capture_variable,
        "assertion_variable": body.assertion_variable, "value_type": body.value_type,
        "enable_advanced_click_types": body.enable_advanced_click_types, "value_source": body.value_source,
        "created_at": _now(),
    }


def _read_template_vars(tid: str) -> list[dict]:
    item = get_table().get_item(
        Key={"pk": f"TEMPLATE#{tid}", "sk": "TEMPLATE_VARIABLES"}
    ).get("Item")
    variables = item.get("variables", []) if item else []
    return [{"key": v.get("key", ""), "value": v.get("value", "")} for v in variables if isinstance(v, dict)]


# ------------------------------------------------------------- template CRUD ---
class TemplateCreate(BaseModel):
    name: str = ""
    description: str = ""


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


@router.get("/templates", dependencies=[Depends(READ)])
def list_templates() -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq("TEMPLATES") & Key("sk").begins_with("TEMPLATE#")
    )
    tpls = [_public(i) for i in resp.get("Items", [])]
    tpls.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return {"templates": tpls}


@router.post("/templates", status_code=status.HTTP_201_CREATED)
def create_template(body: TemplateCreate, principal: Principal = Depends(WRITE)) -> dict:
    tid = str(uuid.uuid4())
    item = {
        "pk": "TEMPLATES", "sk": f"TEMPLATE#{tid}", "id": tid,
        "name": body.name, "description": body.description,
        "created_at": _now(), "created_by": principal.username,
        "version": 1,
    }
    get_table().put_item(Item=item)
    logger.info("template %s created (%r)", tid, body.name)
    return _public(item)


@router.get("/templates/{tid}", dependencies=[Depends(READ)])
def get_template(tid: str) -> dict:
    return _public(_get_template_or_404(tid))


@router.patch("/templates/{tid}", dependencies=[Depends(WRITE)])
def update_template(tid: str, body: TemplateUpdate) -> dict:
    _get_template_or_404(tid)
    provided = body.model_dump(exclude_unset=True)
    if not provided:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    names = {f"#{k}": k for k in provided}
    values = {f":{k}": v for k, v in provided.items()}
    get_table().update_item(
        Key={"pk": "TEMPLATES", "sk": f"TEMPLATE#{tid}"},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in provided),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return _public(_get_template_or_404(tid))


@router.delete("/templates/{tid}", dependencies=[Depends(WRITE)])
def delete_template(tid: str) -> dict:
    _get_template_or_404(tid)
    table = get_table()
    # cascade: steps + variables live under pk="TEMPLATE#<tid>"
    children = table.query(KeyConditionExpression=Key("pk").eq(f"TEMPLATE#{tid}")).get("Items", [])
    for c in children:
        table.delete_item(Key={"pk": c["pk"], "sk": c["sk"]})
    table.delete_item(Key={"pk": "TEMPLATES", "sk": f"TEMPLATE#{tid}"})
    logger.info("template %s deleted (cascade %d child item(s))", tid, len(children))
    return {"status": "deleted", "templateId": tid}


# ------------------------------------------------------------ template steps ---
@router.get("/templates/{tid}/steps", dependencies=[Depends(READ)])
def list_template_steps(tid: str) -> dict:
    _get_template_or_404(tid)
    return {"steps": to_jsonable(_template_steps(tid))}


@router.post("/templates/{tid}/steps", status_code=status.HTTP_201_CREATED, dependencies=[Depends(WRITE)])
def create_template_step(tid: str, body: StepCreate) -> dict:
    _get_template_or_404(tid)
    step = _build_step_item(f"TEMPLATE#{tid}", body)
    get_table().put_item(Item=step)
    _bump_version(tid)
    logger.info("step %s added to template %s (type=%s)", step["id"], tid, body.step_type)
    return to_jsonable(step)


@router.patch("/templates/{tid}/steps/reorder", dependencies=[Depends(WRITE)])
def reorder_template_steps(tid: str, body: ReorderRequest) -> dict:
    if not body.step_orders:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "step_orders is required")
    table_name = get_table().name
    transact = [
        {"Update": {
            "TableName": table_name,
            "Key": {"pk": {"S": f"TEMPLATE#{tid}"}, "sk": {"S": f"STEP#{o.step_id}"}},
            "UpdateExpression": "SET sort = :s",
            "ExpressionAttributeValues": {":s": {"N": str(o.sort)}},
            "ConditionExpression": "attribute_exists(pk)",
        }}
        for o in body.step_orders
    ]
    try:
        get_client().transact_write_items(TransactItems=transact)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("TransactionCanceledException", "ConditionalCheckFailedException"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "One or more steps not found")
        raise
    _bump_version(tid)
    return {"message": "Steps reordered successfully", "count": len(body.step_orders)}


@router.patch("/templates/{tid}/steps/{step_id}", dependencies=[Depends(WRITE)])
def update_template_step(tid: str, step_id: str, body: StepUpdate) -> dict:
    set_parts = ["instruction = :instruction", "step_type = :step_type"]
    values = {":instruction": body.instruction, ":step_type": body.step_type}
    provided = body.model_dump(exclude_unset=True)
    for field in _OPTIONAL_STEP_FIELDS:
        value = provided.get(field)
        if value:
            set_parts.append(f"{field} = :{field}")
            values[f":{field}"] = value
    try:
        get_table().update_item(
            Key={"pk": f"TEMPLATE#{tid}", "sk": f"STEP#{step_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(pk)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Step not found")
        raise
    _bump_version(tid)
    return {"status": "step updated", "stepId": step_id}


@router.delete("/templates/{tid}/steps/{step_id}", dependencies=[Depends(WRITE)])
def delete_template_step(tid: str, step_id: str) -> dict:
    get_table().delete_item(Key={"pk": f"TEMPLATE#{tid}", "sk": f"STEP#{step_id}"})
    _bump_version(tid)
    return {"status": "step deleted", "stepId": step_id}


# -------------------------------------------------------- template variables ---
class VariablesBody(BaseModel):
    variables: list[dict] = []


@router.get("/templates/{tid}/variables", dependencies=[Depends(READ)])
def get_template_variables(tid: str) -> dict:
    _get_template_or_404(tid)
    return {"variables": _read_template_vars(tid)}


@router.post("/templates/{tid}/variables", dependencies=[Depends(WRITE)])
def set_template_variables(tid: str, body: VariablesBody) -> dict:
    _get_template_or_404(tid)
    clean = [{"key": v.get("key", ""), "value": v.get("value", "")} for v in body.variables]
    get_table().put_item(Item={
        "pk": f"TEMPLATE#{tid}", "sk": "TEMPLATE_VARIABLES", "variables": clean, "created_at": _now(),
    })
    return {"variables": clean}


# --------------------------------------------------------------- apply/import ---
def _copy_steps_into(usecase_id: str, tid: str, start_sort: int, now: str) -> int:
    """Copy a template's steps into a use case (new ids, re-sequenced), stamping
    each with a reference to its source template step for later drift/sync."""
    table = get_table()
    tpl_steps = _template_steps(tid)
    for i, st in enumerate(tpl_steps, 1):
        new = {k: v for k, v in st.items() if k not in ("pk", "sk", "id", "created_at")}
        sid = str(uuid.uuid4())
        new.update({
            "pk": f"USECASE#{usecase_id}", "sk": f"STEP#{sid}", "id": sid,
            "sort": start_sort + i, "created_at": now,
            "template_id": tid, "template_step_id": st["id"],
            "template_step_hash": _step_content_hash(st),
        })
        table.put_item(Item=new)
    return len(tpl_steps)


def _merge_template_vars(usecase_id: str, tid: str) -> list[str]:
    """Add a template's variables to a use case, KEEPING the use case's existing
    value on a key clash (non-clobbering). Returns the keys actually added."""
    tpl_vars = _read_template_vars(tid)
    if not tpl_vars:
        return []
    existing = read_variables(usecase_id)
    existing_keys = {v["key"] for v in existing}
    added = [v for v in tpl_vars if v["key"] and v["key"] not in existing_keys]
    if added:
        write_variables(usecase_id, existing + added)
    return [v["key"] for v in added]


class ApplyBody(BaseModel):
    name: str = ""
    starting_url: str = ""


@router.post("/templates/{tid}/apply", status_code=status.HTTP_201_CREATED)
def apply_template(tid: str, body: ApplyBody, principal: Principal = Depends(APPLY)) -> dict:
    """Instantiate a template as a NEW use case (copies steps + variables)."""
    tpl = _get_template_or_404(tid)
    table = get_table()
    uid = str(uuid.uuid4())
    now = _now()
    table.put_item(Item={
        "pk": "USECASES", "sk": f"USECASE#{uid}", "id": uid,
        "name": body.name or f"{tpl.get('name', 'Template')} (from template)",
        "description": tpl.get("description", ""),
        "starting_url": body.starting_url, "active": True, "tags": ["from-template"],
        "created_at": now, "created_by": principal.username,
        "executing_region": DEFAULT_REGION, "model_id": DEFAULT_MODEL,
        "enable_cache": False, "test_platform": "web",
    })
    table.put_item(Item={"pk": f"USECASE#{uid}", "sk": "CREATED_BY",
                         "email": principal.email, "sub": principal.username})
    n = _copy_steps_into(uid, tid, 0, now)
    write_variables(uid, _read_template_vars(tid))
    logger.info("template %s applied -> new usecase %s (%d step(s))", tid, uid, n)
    return {"usecaseId": uid, "steps": n}


class ImportTemplateBody(BaseModel):
    templateId: str


@router.post("/usecase/{usecase_id}/import-template", dependencies=[Depends(APPLY)])
def import_template(usecase_id: str, body: ImportTemplateBody) -> dict:
    """Append a template's steps into an EXISTING use case (after its last step)."""
    if not get_table().get_item(Key={"pk": "USECASES", "sk": f"USECASE#{usecase_id}"}).get("Item"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Use case not found")
    _get_template_or_404(body.templateId)
    existing = get_table().query(
        KeyConditionExpression=Key("pk").eq(f"USECASE#{usecase_id}") & Key("sk").begins_with("STEP#")
    ).get("Items", [])
    max_sort = max((int(s.get("sort", 0)) for s in existing), default=0)
    n = _copy_steps_into(usecase_id, body.templateId, max_sort, _now())
    added_vars = _merge_template_vars(usecase_id, body.templateId)
    logger.info("template %s imported into usecase %s (%d step(s), %d new var(s))",
                body.templateId, usecase_id, n, len(added_vars))
    return {"status": "imported", "usecaseId": usecase_id, "steps": n, "variablesAdded": added_vars}


# ------------------------------------------------ template drift + sync (§4) ---
def _tpl_ref(s: dict) -> dict:
    return to_jsonable({"templateStepId": s.get("id"), "sort": s.get("sort"),
                        "step_type": s.get("step_type"), "instruction": s.get("instruction")})


def _uc_ref(s: dict) -> dict:
    return to_jsonable({"usecaseStepId": s.get("id"), "sort": s.get("sort"),
                        "step_type": s.get("step_type"), "instruction": s.get("instruction")})


def _usecase_steps(usecase_id: str) -> list[dict]:
    return get_table().query(
        KeyConditionExpression=Key("pk").eq(f"USECASE#{usecase_id}") & Key("sk").begins_with("STEP#")
    ).get("Items", [])


@router.get(
    "/usecase/{usecase_id}/template-updates",
    dependencies=[Depends(require_scopes("api/qawb/templates.read"))],
)
def template_updates(usecase_id: str) -> dict:
    """Diff a use case's template-derived steps against their source templates:
    NEW (added upstream), updated (content changed), removed (deleted upstream)."""
    uc_steps = _usecase_steps(usecase_id)
    linked = [s for s in uc_steps if s.get("template_id")]
    out = []
    for tid in sorted({s["template_id"] for s in linked}):
        tpl = get_table().get_item(Key={"pk": "TEMPLATES", "sk": f"TEMPLATE#{tid}"}).get("Item")
        uc_for_tid = [s for s in linked if s["template_id"] == tid]
        uc_by_tsid = {s["template_step_id"]: s for s in uc_for_tid if s.get("template_step_id")}
        if not tpl:  # template deleted entirely
            out.append({"templateId": tid, "templateName": "(deleted)", "templateDeleted": True,
                        "new": [], "updated": [], "removed": [_uc_ref(s) for s in uc_for_tid]})
            continue
        tpl_steps = _template_steps(tid)
        tpl_by_id = {s["id"]: s for s in tpl_steps}
        new = [_tpl_ref(s) for s in tpl_steps if s["id"] not in uc_by_tsid]
        updated, removed = [], []
        for tsid, uc in uc_by_tsid.items():
            ts = tpl_by_id.get(tsid)
            if ts is None:
                removed.append(_uc_ref(uc))
                continue
            synced = uc.get("template_step_hash", "")
            if _step_content_hash(ts) != synced:  # template changed it since sync
                ref = _tpl_ref(ts)
                ref["usecaseStepId"] = uc["id"]
                ref["localEdited"] = _step_content_hash(uc) != synced  # UC also changed it
                updated.append(ref)
        out.append({"templateId": tid, "templateName": tpl.get("name", ""),
                    "new": new, "updated": updated, "removed": removed})
    has_updates = any(t["new"] or t["updated"] or t["removed"] for t in out)
    return {"hasUpdates": has_updates, "templates": out}


class ApplyUpdatesBody(BaseModel):
    templateId: str
    includeUpdates: bool = True  # also overwrite changed steps (may replace local edits)


@router.post("/usecase/{usecase_id}/template-updates/apply", dependencies=[Depends(APPLY)])
def apply_template_updates(usecase_id: str, body: ApplyUpdatesBody) -> dict:
    """Additive sync: append the template's NEW steps; optionally overwrite the
    content of steps the template changed. Never deletes (removed-upstream is
    informational only)."""
    if not get_table().get_item(Key={"pk": "USECASES", "sk": f"USECASE#{usecase_id}"}).get("Item"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Use case not found")
    _get_template_or_404(body.templateId)
    tid = body.templateId
    table = get_table()
    now = _now()

    uc_steps = _usecase_steps(usecase_id)
    uc_by_tsid = {s["template_step_id"]: s for s in uc_steps
                  if s.get("template_id") == tid and s.get("template_step_id")}
    tpl_steps = _template_steps(tid)
    max_sort = max((int(s.get("sort", 0)) for s in uc_steps), default=0)

    added = updated = 0
    for ts in tpl_steps:
        if ts["id"] not in uc_by_tsid:  # NEW → append
            added += 1
            new = {k: v for k, v in ts.items() if k not in ("pk", "sk", "id", "created_at")}
            sid = str(uuid.uuid4())
            new.update({"pk": f"USECASE#{usecase_id}", "sk": f"STEP#{sid}", "id": sid,
                        "sort": max_sort + added, "created_at": now,
                        "template_id": tid, "template_step_id": ts["id"],
                        "template_step_hash": _step_content_hash(ts)})
            table.put_item(Item=new)
        elif body.includeUpdates:  # UPDATE → overwrite content if changed
            uc = uc_by_tsid[ts["id"]]
            if _step_content_hash(ts) != uc.get("template_step_hash", ""):
                item = {k: v for k, v in ts.items() if k not in ("pk", "sk", "id", "created_at")}
                item.update({"pk": uc["pk"], "sk": uc["sk"], "id": uc["id"],
                             "sort": uc.get("sort", 0), "created_at": uc.get("created_at", now),
                             "template_id": tid, "template_step_id": ts["id"],
                             "template_step_hash": _step_content_hash(ts)})
                table.put_item(Item=item)
                updated += 1

    logger.info("template %s synced into usecase %s (+%d new, ~%d updated)", tid, usecase_id, added, updated)
    return {"added": added, "updated": updated}
