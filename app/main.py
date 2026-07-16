"""FastAPI application entry point for the QA Workbench REST API.

Phase 1 scaffold: app wiring, CORS, config validation, and a health check.
Auth routes (login / me) and the authZ middleware arrive in later phases; the
routers/ package is included here but empty for now.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import get_settings
from .logging_config import RequestLogMiddleware, configure_logging
from .routers.nova import config, executions, steps, usecases
from .routers.shell import apps, auth, groups, tokens, users

settings = get_settings()

# Install our logging (correlation id + user, json/plain) before anything logs —
# this also overrides uvicorn's default handlers so every line is consistent.
configure_logging(level=settings.log_level, fmt=settings.log_format)

logger = logging.getLogger("cfins.api")

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

# Added last → outermost: binds the correlation id before anything else runs and
# logs one line per request (with the user, once auth resolves it downstream).
app.add_middleware(RequestLogMiddleware)

# Rejection logging: the request-summary line shows the status (-> 404); these
# handlers add the *reason* so "why was my request rejected?" is answerable from
# the logs. They log, then delegate to FastAPI's default handlers (response is
# unchanged). Both run in-request, so each line carries the correlation id + user.
_reject_logger = logging.getLogger("cfins.request")


@app.exception_handler(StarletteHTTPException)
async def _log_http_exception(request: Request, exc: StarletteHTTPException):
    if exc.status_code >= 400:
        level = logging.ERROR if exc.status_code >= 500 else logging.WARNING
        _reject_logger.log(level, "%s %s rejected: %d %s",
                           request.method, request.url.path, exc.status_code, exc.detail)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _log_validation_error(request: Request, exc: RequestValidationError):
    # Summarize by field location + error type ONLY — never the input values
    # (a validation error can carry a password/secret that failed a constraint).
    fields = [f"{'.'.join(map(str, e.get('loc', [])))}: {e.get('type', '?')}" for e in exc.errors()]
    _reject_logger.warning("%s %s rejected: 422 validation [%s]",
                           request.method, request.url.path, "; ".join(fields))
    return await request_validation_exception_handler(request, exc)

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
