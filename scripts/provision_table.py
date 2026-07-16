#!/usr/bin/env python3
"""
Provision the single DynamoDB table for QA Workbench.

Single-table design (matches sample-qa-studio): one table, `pk`/`sk` composite
keys, plus the `suite-execution-index` GSI. All entities (users, groups, and
later usecases/steps/suites/executions/audit) live in this one table.

Also enables **DynamoDB TTL** on the `ttl` attribute — audit-log items (and any
future ephemeral items) carry `ttl` = a Unix-epoch-seconds expiry, and DynamoDB
auto-deletes them ~within 48h of that time. TTL is enabled on every run.

Idempotent — safe to re-run; it no-ops if the table already exists, and re-checks
TTL each time.

Run locally against real AWS via the cfins-local profile:

    AWS_PROFILE=cfins-local AWS_REGION=us-east-1 \\
        ../.venv/bin/python scripts/provision_table.py

Env:
  WORKBENCH_TABLE   table name (default: cfins-qaworkbench)
  AWS_REGION        region     (default: us-east-1)
  ENABLE_PITR       "true" to enable point-in-time recovery (default: off;
                    local IAM isn't scoped for it — enable in prod)
"""
from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError

TABLE = os.environ.get("WORKBENCH_TABLE", "cfins-qaworkbench")
REGION = os.environ.get("AWS_REGION", "us-east-1")
TTL_ATTR = "ttl"  # must match app/audit.py's item attribute name

ddb = boto3.client("dynamodb", region_name=REGION)


def table_exists() -> bool:
    try:
        ddb.describe_table(TableName=TABLE)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def create_table() -> None:
    print(f"Creating table '{TABLE}' ...")
    ddb.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",  # on-demand; ~$0 at rest, no capacity to manage
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "suite_execution_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "suite-execution-index",
                "KeySchema": [
                    {"AttributeName": "suite_execution_id", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    print("Waiting for table to become ACTIVE ...")
    ddb.get_waiter("table_exists").wait(TableName=TABLE)

    # Point-in-time recovery: prod durability feature; off by default locally
    # (the cfins-local policy isn't scoped for UpdateContinuousBackups).
    if os.environ.get("ENABLE_PITR") == "true":
        try:
            ddb.update_continuous_backups(
                TableName=TABLE,
                PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
            )
            print("Point-in-time recovery enabled.")
        except ClientError as e:
            print(f"WARN: could not enable PITR ({e.response['Error']['Code']}) — skipping.")
    else:
        print("PITR skipped (set ENABLE_PITR=true in prod to enable).")


def ensure_ttl() -> None:
    """Enable TTL on the `ttl` attribute (idempotent, best-effort).

    Skips if already enabled on the right attribute; warns (doesn't fail) if the
    cfins-local policy denies Describe/UpdateTimeToLive — the `ttl` values are
    written regardless, so an admin can enable it later with one CLI call.
    """
    # Already enabled on the right attribute? then nothing to do.
    try:
        desc = ddb.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]
        status, attr = desc.get("TimeToLiveStatus"), desc.get("AttributeName")
        if status in ("ENABLED", "ENABLING"):
            if attr == TTL_ATTR:
                print(f"TTL already {status.lower()} on '{TTL_ATTR}' — nothing to do.")
            else:
                print(f"WARN: TTL is {status.lower()} on a DIFFERENT attribute "
                      f"({attr!r}); expected {TTL_ATTR!r}. Audit items will NOT expire.")
            return
    except ClientError as e:
        # Can't read it (e.g. AccessDenied) — fall through and try to enable anyway.
        if e.response["Error"]["Code"] != "AccessDeniedException":
            print(f"WARN: could not read TTL status ({e.response['Error']['Code']}).")

    try:
        ddb.update_time_to_live(
            TableName=TABLE,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTR},
        )
        print(f"✅ TTL enabled on '{TTL_ATTR}' — items with a past `ttl` epoch auto-expire (~48h).")
    except ClientError as e:
        msg = e.response["Error"].get("Message", "")
        if "already enabled" in msg.lower():
            print(f"TTL already enabled on '{TTL_ATTR}'.")
        else:
            print(f"WARN: could not enable TTL ({e.response['Error']['Code']}). Enable it manually:\n"
                  f"  aws dynamodb update-time-to-live --table-name {TABLE} "
                  f"--time-to-live-specification 'Enabled=true,AttributeName={TTL_ATTR}'")


def main() -> int:
    print(f"Target: table '{TABLE}' in {REGION}")

    if table_exists():
        print(f"Table '{TABLE}' already exists (idempotent).")
    else:
        create_table()
        print(f"Table '{TABLE}' created (pk/sk + suite-execution-index, PAY_PER_REQUEST).")

    # Always (re)check TTL — required by audit-log retention.
    ensure_ttl()
    print("✅ Provisioning complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
