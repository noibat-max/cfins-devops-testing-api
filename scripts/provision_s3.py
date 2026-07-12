#!/usr/bin/env python3
"""
Provision the S3 bucket for QA Workbench execution artifacts.

Execution artifacts (screenshots, video, traces) are stored per-environment in
S3 and referenced by artifact records in DynamoDB. Objects are private and
served only via API-minted presigned URLs — the bucket blocks all public access.

Bucket name is per-environment: cfins-qaworkbench-<env> (e.g. -local, -sat,
-prod), matching the `cfins-qaworkbench*` prefix the cfins-local IAM policy is
scoped to.

Idempotent — safe to re-run; no-ops if the bucket already exists and is ours.
Hardening steps (public-access block, encryption, CORS) are best-effort: if the
local IAM policy lacks a specific permission, the step warns and continues
rather than failing (same pattern as PITR in provision_table.py).

Run locally against real AWS via the cfins-local profile:

    AWS_PROFILE=cfins-local AWS_REGION=us-east-1 \\
        ../.venv/bin/python scripts/provision_s3.py

Env:
  ARTIFACTS_BUCKET  bucket name    (default: cfins-qaworkbench-local)
  AWS_REGION        region         (default: us-east-1)
  CORS_ORIGINS      comma-separated UI origins allowed to GET artifacts in the
                    browser (default: http://localhost:5173)
"""
from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError

BUCKET = os.environ.get("ARTIFACTS_BUCKET", "cfins-qaworkbench-local")
REGION = os.environ.get("AWS_REGION", "us-east-1")
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

s3 = boto3.client("s3", region_name=REGION)


def bucket_status() -> str:
    """Return 'ours' | 'missing' | 'taken' for the target bucket."""
    try:
        s3.head_bucket(Bucket=BUCKET)
        return "ours"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            return "missing"
        if code == "403":
            return "taken"  # exists globally but owned by another account
        raise


def create_bucket() -> None:
    # us-east-1 is special: create_bucket must NOT receive a LocationConstraint
    # (any other region requires it).
    if REGION == "us-east-1":
        s3.create_bucket(Bucket=BUCKET)
    else:
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
    s3.get_waiter("bucket_exists").wait(Bucket=BUCKET)


def harden() -> None:
    # Block ALL public access — artifacts are private, reached via presigned URLs.
    try:
        s3.put_public_access_block(
            Bucket=BUCKET,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print("  · public access blocked")
    except ClientError as e:
        print(f"  WARN: could not set public-access block ({e.response['Error']['Code']}) — skipping.")

    # Default server-side encryption (SSE-S3 / AES256).
    try:
        s3.put_bucket_encryption(
            Bucket=BUCKET,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
        )
        print("  · default encryption enabled (AES256)")
    except ClientError as e:
        print(f"  WARN: could not set encryption ({e.response['Error']['Code']}) — skipping.")

    # CORS: let the browser GET/HEAD artifacts (presigned URLs) from the UI origin.
    # Uploads are done by the CLI (not a browser), so only read methods are needed.
    try:
        s3.put_bucket_cors(
            Bucket=BUCKET,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedMethods": ["GET", "HEAD"],
                        "AllowedOrigins": CORS_ORIGINS,
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        )
        print(f"  · CORS set (GET/HEAD from {', '.join(CORS_ORIGINS)})")
    except ClientError as e:
        print(f"  WARN: could not set CORS ({e.response['Error']['Code']}) — skipping.")


def main() -> int:
    print(f"Target: bucket '{BUCKET}' in {REGION}")

    status = bucket_status()
    if status == "taken":
        print(
            f"❌ Bucket name '{BUCKET}' already exists but is owned by another "
            "account (S3 names are globally unique). Choose a different name."
        )
        return 1
    if status == "ours":
        print(f"✅ Bucket '{BUCKET}' already exists — re-applying hardening (idempotent).")
        harden()
        return 0

    print(f"Creating bucket '{BUCKET}' ...")
    try:
        create_bucket()
    except ClientError as e:
        print(f"❌ Create failed: {e.response['Error']['Code']} — {e.response['Error'].get('Message', '')}")
        return 1

    harden()
    print(f"✅ Bucket '{BUCKET}' is ready (private; presigned-URL access only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
