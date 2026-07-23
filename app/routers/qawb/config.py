"""Use-case config — variables & secrets (§3).

Two independent stores hang off a use case:

* **Variables** — plaintext ``{{key}}`` data, one DynamoDB item per use case
  (``pk="USECASE#<id>"``, ``sk="USECASE_VARIABLES"``). Whole-list replace, matching
  the sample. Values are interpolated into step text by the worker's template parser.

* **Secrets** — sensitive values in **AWS Secrets Manager**, named
  ``<prefix>/usecase/<usecase_id>/<key>``. Listing returns keys/descriptions only,
  never values; a Secret step references a key and the worker types the value in.

Deviation from the sample (approved): secrets are created **untagged** and listed by
name prefix (not tag filters) — keeps the IAM policy small (no TagResource).
"""
from __future__ import annotations

import datetime
import logging

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...aws import get_secrets_client, get_table
from ...config import get_settings
from ...security import require_scopes
from ...serialization import to_jsonable

logger = logging.getLogger("cfins.qawb.config")

router = APIRouter(tags=["usecase-config"])


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _secret_name(usecase_id: str, key: str) -> str:
    return f"{get_settings().secret_prefix}/usecase/{usecase_id}/{key}"


def _secret_prefix(usecase_id: str) -> str:
    return f"{get_settings().secret_prefix}/usecase/{usecase_id}/"


# Secrets Manager reports a missing secret two ways: it never existed
# (ResourceNotFoundException) or it's mid force-delete (InvalidRequestException,
# "marked for deletion"). Both mean "gone" to the caller → 404.
_SM_GONE_CODES = ("ResourceNotFoundException", "InvalidRequestException")


def _raise_if_gone(e: ClientError) -> None:
    if e.response["Error"]["Code"] in _SM_GONE_CODES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Secret not found")


# --------------------------------------------------------------------------- #
# Variables
# --------------------------------------------------------------------------- #
class Variable(BaseModel):
    key: str
    value: str = ""


class VariablesBody(BaseModel):
    variables: list[Variable] = []


class Header(BaseModel):
    name: str
    value: str = ""


class HeadersBody(BaseModel):
    headers: list[Header] = []


# -- reusable data helpers (also used by usecases.py export/import/clone) -- #
def read_variables(usecase_id: str) -> list[dict]:
    """Return a use case's variables as a normalised [{key,value}] list."""
    resp = get_table().get_item(
        Key={"pk": f"USECASE#{usecase_id}", "sk": "USECASE_VARIABLES"}
    )
    item = resp.get("Item")
    variables = item.get("variables", []) if item else []
    return [
        {"key": v.get("key", ""), "value": v.get("value", "")}
        for v in variables
        if isinstance(v, dict)
    ]


def write_variables(usecase_id: str, variables: list[dict]) -> list[dict]:
    """Full-replace a use case's variables item."""
    clean = [{"key": v.get("key", ""), "value": v.get("value", "")} for v in variables]
    get_table().put_item(
        Item={
            "pk": f"USECASE#{usecase_id}",
            "sk": "USECASE_VARIABLES",
            "variables": clean,
            "created_at": _now(),
        }
    )
    return clean


def read_headers(usecase_id: str) -> list[dict]:
    """Return a use case's custom HTTP headers as a [{name,value}] list."""
    resp = get_table().get_item(
        Key={"pk": f"USECASE#{usecase_id}", "sk": "HEADERS"}
    )
    item = resp.get("Item")
    headers = item.get("headers", []) if item else []
    return [
        {"name": h.get("name", ""), "value": h.get("value", "")}
        for h in headers
        if isinstance(h, dict)
    ]


def write_headers(usecase_id: str, headers: list[dict]) -> list[dict]:
    """Full-replace a use case's headers item.

    Deviation from the sample (which stores a {name: value} map): we store an
    ordered [{name,value}] list, consistent with how variables are stored — order
    is preserved and our worker reads the same shape.
    """
    clean = [{"name": h.get("name", ""), "value": h.get("value", "")} for h in headers]
    get_table().put_item(
        Item={
            "pk": f"USECASE#{usecase_id}",
            "sk": "HEADERS",
            "headers": clean,
            "created_at": _now(),
        }
    )
    return clean


def list_secret_meta(usecase_id: str) -> list[dict]:
    """Return a use case's secrets as [{key,description,created_at}] — no values."""
    prefix = _secret_prefix(usecase_id)
    client = get_secrets_client()
    secrets: list[dict] = []
    # Name filter is a prefix match; trailing slash disambiguates uc1 vs uc12.
    paginator = client.get_paginator("list_secrets")
    for page in paginator.paginate(Filters=[{"Key": "name", "Values": [prefix]}]):
        for s in page.get("SecretList", []):
            name = s.get("Name", "")
            if not name.startswith(prefix):
                continue
            created = s.get("CreatedDate")
            secrets.append(
                {
                    "key": name[len(prefix):],
                    "description": s.get("Description", ""),
                    "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ") if created else "",
                }
            )
    secrets.sort(key=lambda x: x["key"])
    return secrets


@router.get(
    "/usecase/{usecase_id}/variables",
    dependencies=[Depends(require_scopes("api/qawb/usecases.read"))],
)
def get_variables(usecase_id: str) -> dict:
    return {"variables": to_jsonable(read_variables(usecase_id))}


@router.post(
    "/usecase/{usecase_id}/variables",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def put_variables(usecase_id: str, body: VariablesBody) -> dict:
    variables = write_variables(usecase_id, [v.model_dump() for v in body.variables])
    logger.info("usecase %s variables set (%d)", usecase_id, len(variables))
    return {"variables": variables}


@router.get(
    "/usecase/{usecase_id}/headers",
    dependencies=[Depends(require_scopes("api/qawb/usecases.read"))],
)
def get_headers(usecase_id: str) -> dict:
    return {"headers": to_jsonable(read_headers(usecase_id))}


@router.post(
    "/usecase/{usecase_id}/headers",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def put_headers(usecase_id: str, body: HeadersBody) -> dict:
    # Values are stored verbatim (static); {{variables}} in them are resolved by
    # the worker at run time via the same template parser as step instructions.
    headers = write_headers(usecase_id, [h.model_dump() for h in body.headers])
    logger.info("usecase %s headers set (%d)", usecase_id, len(headers))
    return {"headers": headers}


# --------------------------------------------------------------------------- #
# Secrets (AWS Secrets Manager)
# --------------------------------------------------------------------------- #
class SecretInput(BaseModel):
    key: str
    value: str
    description: str = ""


class SecretsBody(BaseModel):
    secrets: list[SecretInput] = []


class SecretUpdate(BaseModel):
    secret_key: str
    value: str


class SecretDelete(BaseModel):
    secret_key: str


@router.get(
    "/usecase/{usecase_id}/secrets",
    dependencies=[Depends(require_scopes("api/qawb/usecases.read"))],
)
def list_secrets(usecase_id: str) -> dict:
    return {"secrets": list_secret_meta(usecase_id)}


@router.post(
    "/usecase/{usecase_id}/secrets",
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def create_secrets(usecase_id: str, body: SecretsBody) -> dict:
    client = get_secrets_client()
    set_keys: list[str] = []
    for secret in body.secrets:
        if not secret.key or not secret.value:
            continue  # skip incomplete entries (sample parity)
        name = _secret_name(usecase_id, secret.key)
        description = secret.description or f"Secret for usecase {usecase_id}"
        try:
            client.create_secret(
                Name=name, SecretString=secret.value, Description=description
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceExistsException":
                client.update_secret(SecretId=name, SecretString=secret.value)
            else:
                raise
        set_keys.append(secret.key)
    # Log the KEY names only — never the secret values.
    logger.info("usecase %s secrets set: [%s]", usecase_id, ",".join(set_keys) or "(none)")
    return {"message": "Secrets created/updated successfully", "count": len(set_keys)}


@router.patch(
    "/usecase/{usecase_id}/secrets",
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def update_secret(usecase_id: str, body: SecretUpdate) -> dict:
    if not body.secret_key or not body.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "secret_key and value are required")
    name = _secret_name(usecase_id, body.secret_key)
    try:
        get_secrets_client().update_secret(SecretId=name, SecretString=body.value)
    except ClientError as e:
        _raise_if_gone(e)
        raise
    logger.info("usecase %s secret %r updated", usecase_id, body.secret_key)
    return {"message": "Secret updated successfully", "secret_key": body.secret_key}


@router.delete(
    "/usecase/{usecase_id}/secrets",
    dependencies=[Depends(require_scopes("api/qawb/usecases.write"))],
)
def delete_secret(usecase_id: str, body: SecretDelete) -> dict:
    if not body.secret_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "secret_key is required")
    name = _secret_name(usecase_id, body.secret_key)
    try:
        get_secrets_client().delete_secret(
            SecretId=name, ForceDeleteWithoutRecovery=True
        )
    except ClientError as e:
        _raise_if_gone(e)
        raise
    logger.info("usecase %s secret %r deleted", usecase_id, body.secret_key)
    return {"message": "Secret deleted successfully", "secret_key": body.secret_key}


def delete_all_secrets(usecase_id: str) -> int:
    """Force-delete every secret owned by a use case (cascade on use-case delete).

    Best-effort: individual failures are swallowed so cleanup never blocks the
    parent delete. ListSecrets is eventually consistent, so a secret created
    moments earlier could be missed.
    """
    client = get_secrets_client()
    deleted = 0
    for meta in list_secret_meta(usecase_id):
        try:
            client.delete_secret(
                SecretId=_secret_name(usecase_id, meta["key"]),
                ForceDeleteWithoutRecovery=True,
            )
            deleted += 1
        except ClientError:
            pass
    return deleted


@router.get(
    "/usecase/{usecase_id}/secrets/{secret_key}/value",
    dependencies=[Depends(require_scopes("api/qawb/usecases.read"))],
)
def get_secret_value(usecase_id: str, secret_key: str) -> dict:
    """Worker/internal — the UI never fetches plaintext values."""
    name = _secret_name(usecase_id, secret_key)
    try:
        resp = get_secrets_client().get_secret_value(SecretId=name)
    except ClientError as e:
        _raise_if_gone(e)
        raise
    return {"key": secret_key, "value": resp.get("SecretString", "")}
