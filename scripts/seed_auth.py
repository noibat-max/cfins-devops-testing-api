#!/usr/bin/env python3
"""
Seed auth data into the cfins-qaworkbench table:
  - 3 group items  (admin / author / viewer → scope lists)
  - the admin user (admin / bcrypt("password"), groups=["admin"])

Idempotent + non-destructive: each item is created only if it doesn't already
exist (so re-running won't clobber runtime edits). Set SEED_FORCE=true to
overwrite.

Run:
    AWS_PROFILE=cfins-local AWS_REGION=us-east-1 \\
        ../.venv/bin/python scripts/seed_auth.py
"""
from __future__ import annotations

import datetime
import os
import sys

import bcrypt
import boto3
from botocore.exceptions import ClientError

TABLE = os.environ.get("WORKBENCH_TABLE", "cfins-qaworkbench")
REGION = os.environ.get("AWS_REGION", "us-east-1")
FORCE = os.environ.get("SEED_FORCE") == "true"

table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

# group → scopes (the data-driven mapping; source of truth lives here in the DB)
GROUPS = {
    "admin": {
        "description": "Full access",
        "scopes": ["api/admin"],
    },
    "author": {
        "description": "Create, edit and run tests",
        "scopes": [
            "api/usecases.read", "api/usecases.write", "api/usecases.execute",
            "api/templates.read", "api/templates.write",
            "api/executions.read", "api/executions.write",
            "api/suite.read", "api/suite.write",
        ],
    },
    "viewer": {
        "description": "Read-only: view tests and results, cannot run",
        "scopes": [
            "api/usecases.read", "api/templates.read",
            "api/executions.read", "api/suite.read",
        ],
    },
}

# One representative user per group (password seeded plaintext → bcrypt hash).
USERS = [
    {
        "username": "admin",
        "password": "password",
        "email": "admin@cfins.com",
        "displayName": "Administrator",
        "groups": ["admin"],
    },
    {
        "username": "author",
        "password": "password",
        "email": "author@cfins.com",
        "displayName": "Author User",
        "groups": ["author"],
    },
    {
        "username": "viewer",
        "password": "password",
        "email": "viewer@cfins.com",
        "displayName": "Viewer User",
        "groups": ["viewer"],
    },
]


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def preflight() -> None:
    """Fail fast with a clear message if the profile/region is wrong."""
    try:
        ident = boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as e:  # noqa: BLE001
        sys.exit(
            f"ERROR: no AWS credentials resolved ({e}).\n"
            "  → export AWS_PROFILE=cfins-local  (and AWS_REGION=us-east-1)"
        )
    print(f"AWS account: {ident['Account']}   identity: {ident['Arn']}")
    print(f"Region: {REGION}   Table: {TABLE}")

    ddb = boto3.client("dynamodb", region_name=REGION)
    try:
        ddb.describe_table(TableName=TABLE)
    except ClientError as e:
        code = e.response["Error"]["Code"]  # ResourceNotFound OR AccessDenied (wrong region/account)
        sys.exit(
            f"\nERROR: cannot access table '{TABLE}' in account {ident['Account']} / region {REGION} ({code}).\n"
            "  Likely a wrong profile/region. Run with:\n"
            "    AWS_PROFILE=cfins-local AWS_REGION=us-east-1 ../.venv/bin/python scripts/seed_auth.py\n"
            "  (or run scripts/provision_table.py first if the table doesn't exist)."
        )


def put(item: dict, label: str) -> None:
    kwargs = {"Item": item}
    if not FORCE:
        kwargs["ConditionExpression"] = "attribute_not_exists(pk)"
    try:
        table.put_item(**kwargs)
        print(f"  created  {label}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"  exists   {label}  (skipped; SEED_FORCE=true to overwrite)")
        else:
            raise


def main() -> None:
    preflight()
    print(f"Seeding auth data into '{TABLE}'  force={FORCE}")

    print("Groups:")
    for name, g in GROUPS.items():
        put(
            {
                "pk": "GROUPS",
                "sk": f"GROUP#{name}",
                "name": name,
                "scopes": g["scopes"],
                "description": g["description"],
                "createdAt": now(),
            },
            f"GROUP#{name}",
        )

    print("Users:")
    for u in USERS:
        password_hash = bcrypt.hashpw(u["password"].encode(), bcrypt.gensalt()).decode()
        put(
            {
                "pk": "USERS",
                "sk": f"USER#{u['username']}",
                "username": u["username"],
                "passwordHash": password_hash,
                "email": u["email"],
                "displayName": u["displayName"],
                "groups": u["groups"],
                "status": "active",
                "createdAt": now(),
            },
            f"USER#{u['username']}",
        )

    print("Done.")


if __name__ == "__main__":
    main()
