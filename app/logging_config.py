"""Diagnostic logging: correlation-id + user on every line, JSON or plain.

Two moving parts make every log record self-describing for diagnostics:

  * the **correlation id** lives in a contextvar (`_correlation_id`) that a pure
    ASGI middleware (`RequestLogMiddleware`) sets at the very start of a request —
    in the event-loop context, so it propagates into every downstream context
    (async endpoints AND threadpool-run sync deps/handlers).
  * the **user** isn't known until auth runs, and `get_principal` is a *sync*
    dependency that FastAPI runs in a threadpool with a COPIED context, so a
    contextvar set there can't propagate back to the endpoint or the middleware.
    So `get_principal` records the user in a small map keyed by the correlation id
    (`_user_by_cid`) — the correlation id being the one value reliably present in
    every context. The `ContextFilter` then resolves the user by the current cid,
    so EVERY line (router logs AND the request summary) carries it. The middleware
    drops the entry when the request ends.

  * a `ContextFilter` copies those contextvars onto each `LogRecord`, so the
    formatter can render `correlation_id` / `user` on lines from ANY module —
    ours or uvicorn's — with zero changes at the call sites.

Format + level are env-driven (see config.py): `LOG_FORMAT=json` (default, ideal
for CloudWatch Logs Insights) or `plain` (readable locally); `LOG_LEVEL` tunes
the `cfins.*` loggers. Deployers flip these on the ECS task definition.
"""
from __future__ import annotations

import contextvars
import datetime
import json
import logging
import logging.config
import re
import time
import uuid

# The request/response header carrying the trace id. A client (our CLI) may send
# it so its own logs share the id; absence is the normal case (we generate one).
CORRELATION_HEADER = "X-Correlation-ID"

# Shown when no id/user is bound (pre-auth lines, background tasks, health check).
_UNSET = "-"

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=_UNSET
)

# Correlation id -> username, populated by get_principal (across the threadpool
# boundary a contextvar can't cross) and dropped when the request ends. Dict
# get/set/pop are individually atomic under the GIL, which is all we need.
_user_by_cid: dict[str, str] = {}

# A defensive allowlist for an *incoming* correlation id: opaque token chars only,
# length-capped — so a hostile header can neither inject log lines nor bloat them.
_CID_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")
_CID_MAX_LEN = 64


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize_correlation_id(raw: str | None) -> str | None:
    """Clean an inbound header value; None if nothing usable remains."""
    if not raw:
        return None
    cleaned = _CID_SANITIZE.sub("", raw)[:_CID_MAX_LEN]
    return cleaned or None


def set_correlation_id(value: str) -> contextvars.Token:
    return _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


def set_log_user(username: str) -> None:
    """Record the authenticated user for logging (called from get_principal).

    Keyed by the current correlation id, which is the one value that survives the
    threadpool context copy — so the user resolves on every line of the request,
    not just where get_principal ran.
    """
    cid = _correlation_id.get()
    if cid and cid != _UNSET:
        _user_by_cid[cid] = username or _UNSET


def get_log_user() -> str:
    """The authenticated user bound to the current request (or `-` pre-auth).

    Resolved by correlation id from the map get_principal populates — the same
    source the ContextFilter uses. Read it in-request (e.g. audit middleware,
    before RequestLogMiddleware drops the entry).
    """
    return _user_by_cid.get(_correlation_id.get(), _UNSET)


class ContextFilter(logging.Filter):
    """Inject the current correlation id + user onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        cid = _correlation_id.get()
        record.correlation_id = cid
        record.user = _user_by_cid.get(cid, _UNSET)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — queryable in CloudWatch Logs Insights."""

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc)
        payload = {
            "timestamp": f"{dt:%Y-%m-%dT%H:%M:%S}.{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", _UNSET),
            "user": getattr(record, "user", _UNSET),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_PLAIN_FORMAT = (
    "%(asctime)s %(levelname)-5s %(name)s "
    "cid=%(correlation_id)s user=%(user)s  %(message)s"
)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the app-wide logging config. Call once at startup.

    Overrides uvicorn's default handlers so our `cfins.*` lines AND uvicorn's own
    lines share the same formatter (and thus the correlation id / user).
    """
    formatter = (
        {"()": f"{__name__}.JsonFormatter"}
        if fmt == "json"
        else {"format": _PLAIN_FORMAT, "datefmt": "%Y-%m-%dT%H:%M:%SZ"}
    )
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": f"{__name__}.ContextFilter"}},
            "formatters": {"app": formatter},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "app",
                    "filters": ["context"],
                }
            },
            "loggers": {
                # Our code.
                "cfins": {"level": level, "handlers": ["console"], "propagate": False},
                # Uvicorn: keep server/error lines, silence its access log — our
                # RequestLogMiddleware emits the canonical per-request line instead.
                "uvicorn": {"level": "INFO", "handlers": ["console"], "propagate": False},
                "uvicorn.error": {"level": "INFO", "handlers": ["console"], "propagate": False},
                "uvicorn.access": {"level": "WARNING", "handlers": ["console"], "propagate": False},
            },
            "root": {"level": "WARNING", "handlers": ["console"]},
        }
    )


_req_logger = logging.getLogger("cfins.request")


class RequestLogMiddleware:
    """Pure-ASGI middleware: bind a correlation id, log one line per request.

    Pure ASGI (not BaseHTTPMiddleware) on purpose — it runs the downstream app in
    the SAME context, so the user stamped by get_principal is visible here when we
    emit the summary line. Adds `X-Correlation-ID` to every response; never logs
    bodies, headers, or tokens.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = _header(scope, b"x-correlation-id")
        cid = sanitize_correlation_id(raw) or new_correlation_id()
        tok_c = _correlation_id.set(cid)

        status_holder = {"code": 0}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((b"x-correlation-id", cid.encode("ascii", "ignore")))
            await send(message)

        start = time.perf_counter()
        err: BaseException | None = None
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as e:  # noqa: BLE001 — log, then re-raise unchanged
            err = e
            if not status_holder["code"]:
                status_holder["code"] = 500
            raise
        finally:
            dur_ms = (time.perf_counter() - start) * 1000.0
            method = scope.get("method", "?")
            path = scope.get("path", "?")
            # The user (if auth ran) is resolved by cid in the ContextFilter, so
            # the summary line carries it just like the router lines did.
            line = "%s %s -> %d (%.1fms)"
            args = (method, path, status_holder["code"], dur_ms)
            if err is not None:
                _req_logger.error(line, *args, exc_info=err)
            else:
                _req_logger.info(line, *args)
            _user_by_cid.pop(cid, None)
            _correlation_id.reset(tok_c)


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return None
