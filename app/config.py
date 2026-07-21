"""Central, env-driven configuration (12-factor).

Every tunable the API reads is declared here once, so the rest of the code never
touches os.environ directly. Values come from the process environment; locally
run-local.sh loads them from `.env`, on ECS they come from the task definition.
"""
from __future__ import annotations

import functools
import os


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings:
    """Resolved once at import; simple attribute access everywhere else."""

    # --- Deployment environment (dev/qa/sat/prod; "local" for dev machines) ---
    # Stamped onto PATs at creation and checked at auth, so a token minted in one
    # environment is rejected in another (belt-and-suspenders over the fact that
    # each environment already has its own table).
    environment: str = os.environ.get("ENVIRONMENT", "local")

    # --- Diagnostic logging ---
    # LOG_LEVEL tunes our `cfins.*` loggers (DEBUG for a full trace, WARNING to
    # quiet down). LOG_FORMAT is "json" (default; queryable in CloudWatch Logs
    # Insights) or "plain" (readable locally — set in .env). See logging_config.
    log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_format: str = os.environ.get("LOG_FORMAT", "json").lower()

    # --- AWS / DynamoDB ---
    aws_region: str = os.environ.get("AWS_REGION", "us-east-1")
    workbench_table: str = os.environ.get("WORKBENCH_TABLE", "cfins-qaworkbench")

    # --- Secrets Manager (per-usecase secrets) ---
    # Secret names are "<prefix>/usecase/<usecase_id>/<key>". The worker reads the
    # same name, so this prefix must match its SECRET_PREFIX.
    secret_prefix: str = os.environ.get("SECRET_PREFIX", "cfins-qaworkbench")

    # --- S3 (execution artifacts; per-environment bucket) ---
    # Screenshots/video/traces are stored here and served via presigned URLs.
    # NOTE: presigned URLs MUST be generated with SigV4 (see app/aws.py) — the
    # default SigV2 presign signs Content-Type, which breaks client PUTs.
    artifacts_bucket: str = os.environ.get("ARTIFACTS_BUCKET", "cfins-qaworkbench-local")

    # --- ECS (remote execution: mode `run_now` → ecs.run_task) ---
    # Deploy-time config, one set per environment (these are config, not secrets).
    # Empty when remote runs aren't wired → `run_now` returns a clear 400. Prefer
    # a bare family name for DEV (latest revision) and a pinned family:revision
    # for SAT/prod (so a new task-def registration can't silently change prod).
    ecs_cluster: str = os.environ.get("ECS_CLUSTER", "")
    runner_task_definition: str = os.environ.get("RUNNER_TASK_DEFINITION", "")
    runner_subnets: list[str] = _split_csv(os.environ.get("RUNNER_SUBNETS", ""))
    runner_security_groups: list[str] = _split_csv(os.environ.get("RUNNER_SECURITY_GROUPS", ""))
    runner_launch_type: str = os.environ.get("RUNNER_LAUNCH_TYPE", "FARGATE")
    runner_assign_public_ip: str = os.environ.get("RUNNER_ASSIGN_PUBLIC_IP", "ENABLED")
    # Default artifact capture for remote (run_now) runs: "screenshots" (per-step
    # PNGs) or "full" (adds HTML trace + video to S3). A per-run `capture` in the
    # execute request overrides this.
    runner_capture: str = os.environ.get("RUNNER_CAPTURE", "screenshots")

    @property
    def ecs_enabled(self) -> bool:
        return bool(self.ecs_cluster and self.runner_task_definition and self.runner_subnets)

    # --- Auth / JWT ---
    # HS256 signing key, supplied one of two ways (the direct value wins):
    #   * JWT_SIGN_HASH        — the key itself (local dev via .env).
    #   * JWT_SIGN_HASH_SECRET — a Secrets Manager secret id/ARN; the API fetches
    #                            its value at startup via the task role (prod/ECS).
    # If the SecretString is JSON, set JWT_SIGN_HASH_SECRET_KEY to pick a field;
    # otherwise the whole SecretString is the key. No default on the key itself —
    # a missing key is an obvious failure, not a silently-insecure fixed value.
    # Resolution happens in main._resolve_jwt_sign_hash (fetch + fail-fast).
    jwt_sign_hash: str = os.environ.get("JWT_SIGN_HASH", "")
    jwt_sign_hash_secret: str = os.environ.get("JWT_SIGN_HASH_SECRET", "")
    jwt_sign_hash_secret_key: str = os.environ.get("JWT_SIGN_HASH_SECRET_KEY", "")
    jwt_issuer: str = os.environ.get("JWT_ISSUER", "cfins-qaworkbench")
    jwt_ttl_hours: int = int(os.environ.get("JWT_TTL_HOURS", "8"))

    # --- CORS ---
    cors_origins: list[str] = _split_csv(
        os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    )

    # --- Cognito (SSO provider; empty when SSO isn't configured) ---
    cognito_user_pool_id: str = os.environ.get("COGNITO_USER_POOL_ID", "")
    cognito_client_id: str = os.environ.get("COGNITO_CLIENT_ID", "")
    # Managed-login domain base URL, e.g. https://<prefix>.auth.<region>.amazoncognito.com
    cognito_domain: str = os.environ.get("COGNITO_DOMAIN", "").rstrip("/")

    @property
    def cognito_enabled(self) -> bool:
        return bool(self.cognito_user_pool_id and self.cognito_client_id)

    @property
    def cognito_issuer(self) -> str:
        """The `iss` value Cognito stamps on tokens from this pool."""
        if not self.cognito_user_pool_id:
            return ""
        return (
            f"https://cognito-idp.{self.aws_region}.amazonaws.com/"
            f"{self.cognito_user_pool_id}"
        )

    @property
    def cognito_jwks_url(self) -> str:
        return f"{self.cognito_issuer}/.well-known/jwks.json" if self.cognito_issuer else ""

    @property
    def cognito_userinfo_url(self) -> str:
        return f"{self.cognito_domain}/oauth2/userInfo" if self.cognito_domain else ""


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
