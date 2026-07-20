"""boto3 access helpers.

One place constructs AWS clients/resources so credentials and region are
resolved consistently. We rely on boto3's *default credential chain* — no
profile or keys are hardcoded — so the exact same code runs:

  * locally  → AWS_PROFILE=cfins-local resolves from ~/.aws
  * on ECS   → the task role is resolved from the container credentials endpoint

Clients are cached so we don't rebuild a session per request.
"""
from __future__ import annotations

import functools

import boto3
from botocore.config import Config

from .config import get_settings


@functools.lru_cache
def _session() -> boto3.Session:
    # No profile/keys passed in — default chain does the right thing everywhere.
    return boto3.Session(region_name=get_settings().aws_region)


@functools.lru_cache
def get_table():
    """The single QA Workbench DynamoDB table (single-table design)."""
    return _session().resource("dynamodb").Table(get_settings().workbench_table)


@functools.lru_cache
def get_client():
    """Low-level DynamoDB client for APIs the resource doesn't expose (e.g.
    transact_write_items). NOTE: this expects/returns *typed* AttributeValues
    ({"S": ...}); do NOT confuse it with `get_table().meta.client`, which is the
    resource's document-interface client that auto-serializes native types.
    """
    return _session().client("dynamodb")


@functools.lru_cache
def get_secrets_client():
    """AWS Secrets Manager client for per-usecase secrets."""
    return _session().client("secretsmanager")


@functools.lru_cache
def get_s3_client():
    """S3 client for execution artifacts, configured for **SigV4** presigning.

    SigV4 is REQUIRED, not optional: the default (SigV2) presigned URL bakes the
    `Content-Type` into the signature, so a client PUT that sends any content
    type fails with SignatureDoesNotMatch (403). SigV4 doesn't sign Content-Type
    unless explicitly included, so the CLI can upload with any type. Verified
    end-to-end against the real bucket — see CLAUDE.md §5 / scripts/provision_s3.py.
    """
    return _session().client("s3", config=Config(signature_version="s3v4"))


@functools.lru_cache
def get_ecs_client():
    """ECS client for launching remote (run_now) worker tasks via ecs.run_task."""
    return _session().client("ecs")
