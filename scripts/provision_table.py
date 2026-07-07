#!/usr/bin/env python3
"""
Provision the single DynamoDB table for QA Workbench.

Single-table design (matches sample-qa-studio): one table, `pk`/`sk` composite
keys, plus the `suite-execution-index` GSI. All entities (users, groups, and
later usecases/steps/suites/executions) live in this one table.

Idempotent — safe to re-run; it no-ops if the table already exists.

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

ddb = boto3.client("dynamodb", region_name=REGION)


def table_exists() -> bool:
    try:
        ddb.describe_table(TableName=TABLE)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def main() -> int:
    print(f"Target: table '{TABLE}' in {REGION}")

    if table_exists():
        print(f"✅ Table '{TABLE}' already exists — nothing to do (idempotent).")
        return 0

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

    print(f"✅ Table '{TABLE}' is ready (pk/sk + suite-execution-index, PAY_PER_REQUEST).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
