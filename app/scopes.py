"""The fixed scope catalog.

Scopes are defined by the code (each endpoint declares its required scope via
require_scopes), NOT created at runtime — so this is the authoritative list of
scopes a group can be granted. It changes only when endpoints are added.
Mirrors docs/api-surface.md.
"""
from __future__ import annotations

SCOPE_CATALOG: list[dict] = [
    {"scope": "api/admin", "description": "Full access — inherits every scope"},
    {"scope": "api/qawb/usecases.read", "description": "View use cases and steps"},
    {"scope": "api/qawb/usecases.write", "description": "Create and edit use cases and steps"},
    {"scope": "api/qawb/usecases.execute", "description": "Run use cases"},
    {"scope": "api/qawb/templates.read", "description": "View templates"},
    {"scope": "api/qawb/templates.write", "description": "Create and edit templates"},
    {"scope": "api/qawb/executions.read", "description": "View executions and results"},
    {"scope": "api/qawb/executions.write", "description": "Manage executions (stop, artifacts)"},
    {"scope": "api/qawb/suite.read", "description": "View test suites"},
    {"scope": "api/qawb/suite.write", "description": "Create, edit and run test suites"},
    {"scope": "api/qawb/schedules.read", "description": "View schedules"},
    {"scope": "api/qawb/schedules.write", "description": "Create, edit and delete schedules"},
]

VALID_SCOPES = {s["scope"] for s in SCOPE_CATALOG}
ADMIN_SCOPE = "api/admin"
