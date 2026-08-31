"""Linear OAuth endpoints.

The browser-facing half of the flow only. All token handling lives in
`app/services/linear_auth_service.py`, deliberately away from agent and review
logic.
"""

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.errors import LinearError
from app.core.logging import get_logger
from app.services.linear_auth_service import LinearAuthService

logger = get_logger(__name__)

router = APIRouter(prefix="/auth/linear", tags=["auth"])


def _auth_service(request: Request) -> LinearAuthService:
    """Pull the auth service off the composition root."""
    return request.app.state.linear_auth_service


class InstallResponse(BaseModel):
    """Where the browser should go to authorize AIDA-MATE."""

    authorization_url: str


class CallbackResponse(BaseModel):
    """Result of a completed installation.

    Reports identity only. No token, not even truncated, appears here.
    """

    connected: bool
    organization_id: str
    organization_name: str | None = None
    actor_id: str
    scopes: list[str]


@router.get("/install")
async def install(
    request: Request,
    redirect: bool = Query(
        default=True,
        description="Redirect straight to Linear (browser). Set false to receive the URL as JSON.",
    ),
):
    """Begin the OAuth flow.

    Uses `actor=app` so AIDA-MATE installs as an app user and can be delegated
    issues, rather than acting as the authorizing human.
    """
    try:
        auth_request = await _auth_service(request).build_authorization_request(actor="app")
    except LinearError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if redirect:
        return RedirectResponse(url=auth_request.authorization_url, status_code=status.HTTP_302_FOUND)
    return InstallResponse(authorization_url=auth_request.authorization_url)


@router.get("/callback", response_model=CallbackResponse)
async def callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> CallbackResponse:
    """Handle Linear's redirect and exchange the code for tokens.

    A user who declines consent arrives here with `error` and no `code`; that is
    an expected outcome, reported as 400 rather than logged as a fault.
    """
    if error:
        logger.info("Linear authorization was declined or failed", extra={"oauth_error": error})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Linear authorization failed: {error_description or error}",
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing 'code' or 'state' in callback.",
        )

    try:
        installation = await _auth_service(request).complete_authorization(code=code, state=state)
    except LinearError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return CallbackResponse(
        connected=True,
        organization_id=installation.organization_id,
        organization_name=installation.organization_name,
        actor_id=installation.actor_id,
        scopes=installation.scopes,
    )


@router.get("/status")
async def installation_status(request: Request) -> dict[str, object]:
    """List authorized workspaces.

    Identity and scope only — never tokens or expiry secrets.
    """
    installations = await request.app.state.linear_token_store.list_all()
    return {
        "count": len(installations),
        "installations": [
            {
                "organization_id": item.organization_id,
                "organization_name": item.organization_name,
                "actor_id": item.actor_id,
                "scopes": item.scopes,
                "expired": item.is_expired(),
            }
            for item in installations
        ],
    }
