#!/usr/bin/env python3
"""
Provision the JWT (HS256) signing key in AWS Secrets Manager.

The API resolves its signing key at startup: if JWT_SIGN_HASH is unset it fetches
JWT_SIGN_HASH_SECRET from Secrets Manager via its task role (see app/config.py +
main._resolve_jwt_sign_hash). This script creates that secret, one per
environment, holding a cryptographically-random value.

Secret name is per-environment: <SECRET_PREFIX>/<ENVIRONMENT>/jwt-sign-hash
(e.g. cfins-qaworkbench/local/jwt-sign-hash). Naming it under the
`cfins-qaworkbench*` prefix means the API task role (and the cfins-local IAM
policy) can already read it with NO policy change.

Idempotent + non-destructive: if the secret already exists it is left untouched
(the value is NOT rotated) unless SECRET_FORCE=true, which stores a fresh value
via put_secret_value. The secret VALUE is never printed — only its name/ARN and
the `JWT_SIGN_HASH_SECRET=` line to set.

Run locally against real AWS via the cfins-local profile:

    AWS_PROFILE=cfins-local AWS_REGION=us-east-1 ENVIRONMENT=local \\
        ../.venv/bin/python scripts/provision_jwt_secret.py

Env:
  SECRET_PREFIX          name prefix        (default: cfins-qaworkbench)
  ENVIRONMENT            env segment        (default: local)
  JWT_SECRET_NAME        full name override (default: <prefix>/<env>/jwt-sign-hash)
  AWS_REGION             region             (default: us-east-1)
  JWT_SIGN_HASH_BYTES    entropy in bytes   (default: 48 = 384 bits)
  SECRET_FORCE=true      rotate the value if the secret already exists
"""
from __future__ import annotations

import os
import secrets
import sys

import boto3
from botocore.exceptions import ClientError

PREFIX = os.environ.get("SECRET_PREFIX", "cfins-qaworkbench")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
NAME = os.environ.get("JWT_SECRET_NAME", f"{PREFIX}/{ENVIRONMENT}/jwt-sign-hash")
REGION = os.environ.get("AWS_REGION", "us-east-1")
BYTES = int(os.environ.get("JWT_SIGN_HASH_BYTES", "48"))
FORCE = os.environ.get("SECRET_FORCE") == "true"

sm = boto3.client("secretsmanager", region_name=REGION)


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
    print(f"Region: {REGION}   Secret: {NAME}")


def find() -> str | None:
    """Return the secret's ARN if it exists, else None."""
    try:
        return sm.describe_secret(SecretId=NAME)["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def new_value() -> str:
    """A URL-safe random signing key (>= 256 bits; default 384)."""
    return secrets.token_urlsafe(BYTES)


def done(arn: str) -> None:
    """Print the wiring hint (never the value)."""
    print("\nSet the API to use it (leave JWT_SIGN_HASH blank):")
    print(f"  JWT_SIGN_HASH_SECRET={NAME}")
    print(f"  # ARN: {arn}")


def main() -> int:
    preflight()

    arn = find()
    if arn and not FORCE:
        print(f"✅ Secret '{NAME}' already exists — leaving the value untouched "
              "(SECRET_FORCE=true to rotate).")
        done(arn)
        return 0

    if arn and FORCE:
        print(f"Rotating value for existing secret '{NAME}' (SECRET_FORCE=true) ...")
        try:
            sm.put_secret_value(SecretId=NAME, SecretString=new_value())
        except ClientError as e:
            print(f"❌ Rotate failed: {e.response['Error']['Code']} — "
                  f"{e.response['Error'].get('Message', '')}")
            return 1
        print("  · new value stored (previous version retained as AWSPREVIOUS)")
        done(arn)
        return 0

    print(f"Creating secret '{NAME}' ...")
    try:
        resp = sm.create_secret(
            Name=NAME,
            Description=f"QA Workbench API HS256 signing key ({ENVIRONMENT})",
            SecretString=new_value(),
        )
    except ClientError as e:
        print(f"❌ Create failed: {e.response['Error']['Code']} — "
              f"{e.response['Error'].get('Message', '')}")
        return 1

    print("  · created (raw SecretString — leave JWT_SIGN_HASH_SECRET_KEY empty)")
    done(resp["ARN"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
