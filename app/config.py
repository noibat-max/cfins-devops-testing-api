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

    # --- AWS / DynamoDB ---
    aws_region: str = os.environ.get("AWS_REGION", "us-east-1")
    workbench_table: str = os.environ.get("WORKBENCH_TABLE", "cfins-qaworkbench")

    # --- Secrets Manager (per-usecase secrets) ---
    # Secret names are "<prefix>/usecase/<usecase_id>/<key>". The worker reads the
    # same name, so this prefix must match its SECRET_PREFIX.
    secret_prefix: str = os.environ.get("SECRET_PREFIX", "cfins-qaworkbench")

    # --- Auth / JWT ---
    # No default for the secret on purpose — a missing secret should be an
    # obvious failure, not a silently-insecure fixed key. Validated in main.
    jwt_secret: str = os.environ.get("JWT_SECRET", "")
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
