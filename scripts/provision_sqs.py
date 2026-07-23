#!/usr/bin/env python3
"""
Provision the SQS queue for queued ("run later") executions.

Mode `queued` on POST /usecase/{id}/execute enqueues {execution_id, usecase_id,
capture} onto this queue; a scheduled dispatcher Lambda drains it and launches
runner tasks up to the concurrency cap (see the CLI repo's dispatcher). This
script creates:

  * cfins-qaworkbench-executions      — the work queue
  * cfins-qaworkbench-executions-dlq  — dead-letter queue (redrive target)

Names sit under the `cfins-qaworkbench-*` prefix so the existing IAM scoping
covers them. Idempotent — re-running reconciles attributes; it no-ops if nothing
changed.

Because the dispatcher is PULL-based (it only receives a message when it has a
free slot), there's no capacity-bounce inflating ReceiveCount, so maxReceiveCount
can be modest — the DLQ only catches genuine repeated launch failures.

Run locally against real AWS via the cfins-local profile:

    AWS_PROFILE=cfins-local AWS_REGION=us-east-1 \\
        ../.venv/bin/python scripts/provision_sqs.py

Env:
  QUEUE_NAME          work queue name   (default: cfins-qaworkbench-executions)
  DLQ_NAME            DLQ name          (default: <QUEUE_NAME>-dlq)
  AWS_REGION          region            (default: us-east-1)
  VISIBILITY_TIMEOUT  seconds a received msg is hidden (default: 60)
  MAX_RECEIVE_COUNT   deliveries before a msg goes to the DLQ (default: 5)
  RETENTION_SECONDS   message retention (default: 1209600 = 14 days)
  RECEIVE_WAIT        default long-poll wait seconds (default: 20)
"""
from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
QUEUE_NAME = os.environ.get("QUEUE_NAME", "cfins-qaworkbench-executions")
DLQ_NAME = os.environ.get("DLQ_NAME", f"{QUEUE_NAME}-dlq")
VISIBILITY_TIMEOUT = os.environ.get("VISIBILITY_TIMEOUT", "60")
MAX_RECEIVE_COUNT = os.environ.get("MAX_RECEIVE_COUNT", "5")
RETENTION_SECONDS = os.environ.get("RETENTION_SECONDS", "1209600")
RECEIVE_WAIT = os.environ.get("RECEIVE_WAIT", "20")

sqs = boto3.client("sqs", region_name=REGION)


def preflight() -> None:
    try:
        ident = boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as e:  # noqa: BLE001
        sys.exit(
            f"ERROR: no AWS credentials resolved ({e}).\n"
            "  → export AWS_PROFILE=cfins-local  (and AWS_REGION=us-east-1)"
        )
    print(f"AWS account: {ident['Account']}   identity: {ident['Arn']}")
    print(f"Region: {REGION}   Queue: {QUEUE_NAME}   DLQ: {DLQ_NAME}")


def _denied(e: ClientError) -> bool:
    return e.response["Error"]["Code"] in ("AccessDenied", "AccessDeniedException",
                                           "KMS.AccessDeniedException")


def ensure_queue(name: str, attributes: dict) -> tuple[str, str]:
    """Get-or-create the queue; reconcile attributes; return (url, arn)."""
    try:
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
        if attributes:
            sqs.set_queue_attributes(QueueUrl=url, Attributes=attributes)
            print(f"  reconciled  {name}")
        else:
            print(f"  exists      {name}")
    except ClientError as e:
        if e.response["Error"]["Code"].endswith("NonExistentQueue"):
            url = sqs.create_queue(QueueName=name, Attributes=attributes)["QueueUrl"]
            print(f"  created     {name}")
        else:
            raise
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    return url, arn


def main() -> int:
    preflight()
    try:
        # 1. DLQ first (no redrive of its own).
        _, dlq_arn = ensure_queue(DLQ_NAME, {"MessageRetentionPeriod": RETENTION_SECONDS})

        # 2. Work queue, redriving to the DLQ.
        q_url, q_arn = ensure_queue(
            QUEUE_NAME,
            {
                "VisibilityTimeout": VISIBILITY_TIMEOUT,
                "MessageRetentionPeriod": RETENTION_SECONDS,
                "ReceiveMessageWaitTimeSeconds": RECEIVE_WAIT,
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": int(MAX_RECEIVE_COUNT)}
                ),
            },
        )
    except ClientError as e:
        if _denied(e):
            print(f"\n❌ AccessDenied ({e.operation_name}). The cfins-local policy needs SQS on "
                  "cfins-qaworkbench-* — add this statement and re-run:\n")
            print(json.dumps({
                "Sid": "SqsQueuedExecutions",
                "Effect": "Allow",
                "Action": [
                    "sqs:CreateQueue", "sqs:GetQueueUrl", "sqs:GetQueueAttributes",
                    "sqs:SetQueueAttributes", "sqs:SendMessage",
                ],
                "Resource": f"arn:aws:sqs:{REGION}:*:cfins-qaworkbench-*",
            }, indent=2))
            return 1
        raise

    print(f"\n✅ Queue ready.  ARN: {q_arn}")
    print("Set the API to enqueue to it:")
    print(f"  QAWB_SQS_QUEUE_URL={q_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
