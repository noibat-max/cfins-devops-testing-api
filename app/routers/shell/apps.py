"""Application catalog for the workbench landing page.

Hardcoded for now. When there's an app-management screen this moves to a
data-driven catalog in DynamoDB. No auth required — the landing page fetches
this to render the app cards.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["apps"])


class WorkbenchApp(BaseModel):
    id: str
    name: str
    shortName: str
    description: str
    category: str
    route: str
    status: str  # "available" | "coming-soon"
    icon: str  # Cloudscape icon name (placeholder per-app icon)


_APPS: list[WorkbenchApp] = [
    WorkbenchApp(
        id="qa-studio",
        name="QA Workbench - Nova Act",
        shortName="QA Workbench",
        description=(
            "Agentic, plain-English UI testing. Amazon Nova Act drives a managed "
            "browser to execute and validate test steps."
        ),
        category="Functional UI Testing",
        route="/apps/qa-studio",
        status="available",
        icon="script",
    ),
    WorkbenchApp(
        id="dlt",
        name="AWS Distributed Load Testing",
        shortName="Distributed Load Testing",
        description=(
            "Run distributed load & performance tests at scale and review "
            "throughput, latency and error metrics."
        ),
        category="Performance Testing",
        route="/apps/dlt",
        status="available",
        icon="multiscreen",
    ),
]


@router.get("/apps", response_model=list[WorkbenchApp])
def list_apps() -> list[WorkbenchApp]:
    return _APPS
