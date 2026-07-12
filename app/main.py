"""FastAPI application entry point for the QA Workbench REST API.

Phase 1 scaffold: app wiring, CORS, config validation, and a health check.
Auth routes (login / me) and the authZ middleware arrive in later phases; the
routers/ package is included here but empty for now.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers.nova import config, executions, steps, usecases
from .routers.shell import apps, auth, groups, tokens, users

logger = logging.getLogger("cfins.api")

settings = get_settings()

app = FastAPI(
    title="CFINS QA Workbench API",
    description="C&F unified testing platform — lift-and-shift of Nova Act QA Studio.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Route namespacing (app-owns-/api): every functional route is served under
# `/api`; each hosted application gets its own second segment. QA Studio (Nova
# Act) lives under `/api/nova`; the workbench shell (auth, apps, admin, tokens)
# is app-agnostic and sits directly under `/api`. `/health` + `/docs` stay at
# root (load-balancer / Swagger convention). Future apps: `/api/dlt`, ...
API = "/api"
NOVA = f"{API}/nova"

# Workbench shell — app-agnostic platform routes.
app.include_router(auth.router, prefix=API)
app.include_router(apps.router, prefix=API)
app.include_router(users.router, prefix=API)
app.include_router(groups.router, prefix=API)
app.include_router(tokens.router, prefix=API)

# QA Studio (Nova Act) application.
app.include_router(usecases.router, prefix=NOVA)
app.include_router(steps.router, prefix=NOVA)
app.include_router(config.router, prefix=NOVA)
app.include_router(executions.router, prefix=NOVA)


@app.on_event("startup")
def _validate_config() -> None:
    """Fail loudly on obvious misconfiguration rather than at first request."""
    if not settings.jwt_secret:
        logger.warning(
            "JWT_SECRET is not set — token minting will fail. "
            "Set it in .env (see .env.example) before using auth endpoints."
        )
    logger.info(
        "QA Workbench API starting — region=%s table=%s cors=%s",
        settings.aws_region,
        settings.workbench_table,
        settings.cors_origins,
    )


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness check — no auth, no AWS calls. Proves the server booted."""
    return {"status": "ok"}
