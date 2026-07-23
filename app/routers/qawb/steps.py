"""Steps — a use case's ordered test steps (§2).

Single-table: step items live under their use case —
  pk="USECASE#<usecase_id>"  sk="STEP#<step_id>"

Ports create/list/update/delete/reorder. `update-from-template` is deferred with
Templates (§7). Stored attribute names match the sample (snake_case) so the
worker reads the same shape.
"""
from __future__ import annotations

import datetime
import logging
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...aws import get_client, get_table
from ...security import require_scopes
from ...serialization import to_jsonable

logger = logging.getLogger("cfins.qawb.steps")

router = APIRouter(tags=["steps"])

# Optional step attributes updated only when truthy (matches sample).
_OPTIONAL_STEP_FIELDS = (
    "secret_key",
    "validation_type",
    "validation_operator",
    "validation_value",
    "validation_tolerance",
    "capture_variable",
    "assertion_variable",
    "value_type",
    "enable_advanced_click_types",
    "value_source",
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StepCreate(BaseModel):
    sort: int
    instruction: str
    step_type: str = "navigation"
    secret_key: str = ""
    validation_type: str = ""
    validation_operator: str = ""
    validation_value: str = ""
    validation_tolerance: str = ""
    capture_variable: str = ""
    assertion_variable: str = ""
    value_type: str = ""
    enable_advanced_click_types: bool = False
    value_source: str = ""


class StepUpdate(BaseModel):
    instruction: str
    step_type: str
    secret_key: str | None = None
    validation_type: str | None = None
    validation_operator: str | None = None
    validation_value: str | None = None
    validation_tolerance: str | None = None
    capture_variable: str | None = None
    assertion_variable: str | None = None
    value_type: str | None = None
    enable_advanced_click_types: bool | None = None
    value_source: str | None = None


class StepOrder(BaseModel):
    step_id: str  # bare id; we build the STEP#<id> key
    sort: int


class ReorderRequest(BaseModel):
    step_orders: list[StepOrder]


@router.get(
    "/usecase/{usecase_id}/steps",
    dependencies=[Depends(require_scopes("api/qawb/usecases.read"))],
)
def list_steps(usecase_id: str) -> dict:
    resp = get_table().query(
        KeyConditionExpression=Key("pk").eq(f"USECASE#{usecase_id}")
        & Key("sk").begins_with("STEP#")
    )
    steps = resp.get("Items", [])
    steps.sort(key=lambda s: s.get("sort", 0))
    for step in steps:
        # Cache fields are read-only; expose null when absent (sample parity).
        step.setdefault("cached_steps", None)
        step.setdefault("cache_last_updated", None)
    return {"steps": to_jsonable(steps)}


@router.post(
    "/usecase/{usecase_id}/steps",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def create_step(usecase_id: str, body: StepCreate) -> dict:
    step_id = str(uuid.uuid4())
    step = {
        "pk": f"USECASE#{usecase_id}",
        "sk": f"STEP#{step_id}",
        "id": step_id,
        "sort": body.sort,
        "instruction": body.instruction,
        "step_type": body.step_type,
        "secret_key": body.secret_key,
        "validation_type": body.validation_type,
        "validation_operator": body.validation_operator,
        "validation_value": body.validation_value,
        "validation_tolerance": body.validation_tolerance,
        "capture_variable": body.capture_variable,
        "assertion_variable": body.assertion_variable,
        "value_type": body.value_type,
        "enable_advanced_click_types": body.enable_advanced_click_types,
        "value_source": body.value_source,
        "created_at": _now(),
    }
    get_table().put_item(Item=step)
    logger.info("step %s added to usecase %s (type=%s)", step["id"], usecase_id, body.step_type)
    return to_jsonable(step)


@router.patch(
    "/usecase/{usecase_id}/steps/reorder",
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def reorder_steps(usecase_id: str, body: ReorderRequest) -> dict:
    if not body.step_orders:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "step_orders is required")

    table_name = get_table().name
    # Atomic: all sorts update together (matches sample's transaction).
    # Uses the low-level client (typed AttributeValues), NOT the resource's.
    transact_items = [
        {
            "Update": {
                "TableName": table_name,
                "Key": {
                    "pk": {"S": f"USECASE#{usecase_id}"},
                    "sk": {"S": f"STEP#{o.step_id}"},
                },
                "UpdateExpression": "SET sort = :s",
                "ExpressionAttributeValues": {":s": {"N": str(o.sort)}},
                "ConditionExpression": "attribute_exists(pk)",
            }
        }
        for o in body.step_orders
    ]
    try:
        get_client().transact_write_items(TransactItems=transact_items)
    except ClientError as e:
        if e.response["Error"]["Code"] in (
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "One or more steps not found")
        raise

    logger.info("reordered %d step(s) for usecase %s", len(body.step_orders), usecase_id)
    return {"message": "Steps reordered successfully", "count": len(body.step_orders)}


@router.patch(
    "/usecase/{usecase_id}/steps/{step_id}",
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def update_step(usecase_id: str, step_id: str, body: StepUpdate) -> dict:
    set_parts = ["instruction = :instruction", "step_type = :step_type"]
    values = {":instruction": body.instruction, ":step_type": body.step_type}

    provided = body.model_dump(exclude_unset=True)
    for field in _OPTIONAL_STEP_FIELDS:
        value = provided.get(field)
        if value:  # sample only updates truthy optionals
            set_parts.append(f"{field} = :{field}")
            values[f":{field}"] = value

    try:
        get_table().update_item(
            Key={"pk": f"USECASE#{usecase_id}", "sk": f"STEP#{step_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(pk)",  # 404 instead of upsert
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Step not found")
        raise

    return {"status": "step updated", "stepId": step_id}


@router.delete(
    "/usecase/{usecase_id}/steps/{step_id}",
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def delete_step(usecase_id: str, step_id: str) -> dict:
    get_table().delete_item(
        Key={"pk": f"USECASE#{usecase_id}", "sk": f"STEP#{step_id}"}
    )
    logger.info("step %s deleted from usecase %s", step_id, usecase_id)
    return {"status": "step deleted", "stepId": step_id}
