"""Health and readiness endpoints."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Liveness response."""

    status: str


class ReadinessResponse(BaseModel):
    """Readiness response.

    Reports which capabilities are actually configured. `sandbox` is the one
    that gates real work: without it, review jobs fail closed, because
    executing untrusted repository content on the API host is not an
    acceptable fallback.
    """

    status: str
    sandbox: bool
    github: bool
    linear: bool


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe. 200 as soon as the process is up and configured."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse)
async def ready(request: Request) -> ReadinessResponse:
    """Readiness probe reporting configured capabilities.

    Deliberately reports booleans, never credential values.
    """
    settings = request.app.state.settings
    sandbox_ok = settings.sandbox_configured
    return ReadinessResponse(
        status="ready" if sandbox_ok else "degraded",
        sandbox=sandbox_ok,
        github=settings.github_app_configured or bool(settings.github_dev_token),
        linear=settings.linear_oauth_configured or bool(settings.linear_dev_api_key),
    )
