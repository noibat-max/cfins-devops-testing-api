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

from .config import get_settings


@functools.lru_cache
def _session() -> boto3.Session:
    # No profile/keys passed in — default chain does the right thing everywhere.
    return boto3.Session(region_name=get_settings().aws_region)


@functools.lru_cache
def get_table():
    """The single QA Workbench DynamoDB table (single-table design)."""
    return _session().resource("dynamodb").Table(get_settings().workbench_table)
