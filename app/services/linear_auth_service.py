"""Linear OAuth 2.0.

AIDA-MATE is a multi-workspace integration, so it must never be wired to one
developer's personal API key. This module owns the whole authorization
lifecycle — authorize URL, code exchange, refresh, revoke — and nothing else.
No agent logic, no review logic, no GitHub.

Flow specifics verified against https://linear.app/developers/oauth-2-0-authentication:

* authorize:  https://linear.app/oauth/authorize
* token:      https://api.linear.app/oauth/token   (form-encoded)
* revoke:     https://api.linear.app/oauth/revoke
* scopes are **comma**-separated, not space-separated
* `actor=app` installs AIDA-MATE as an app user rather than acting as the
  authorizing human — required for it to be delegated issues as an agent
* access tokens last ~24h and come with a refresh token

Three security properties this module is responsible for:

1. **CSRF** — `state` is random, server-recorded, single-use, and expiring.
2. **Code interception** — PKCE (S256) is used even though AIDA-MATE is a
   confidential client, per OAuth 2.1 guidance.
3. **Credential hygiene** — tokens are `SecretStr` and are never logged, not
   even at DEBUG.
"""

import asyncio
import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import LinearError
from app.core.logging import get_logger
from app.models.linear import AuthorizationRequest, LinearInstallation, OAuthState
from app.services.linear_service import LinearGraphQLClient, fetch_viewer
from app.services.token_store import FileLinearTokenStore, InMemoryLinearTokenStore, InMemoryOAuthStateStore

logger = get_logger(__name__)

LINEAR_AUTHORIZE_URL = "https://linear.app/oauth/authorize"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_REVOKE_URL = "https://api.linear.app/oauth/revoke"

_DEFAULT_TOKEN_LIFETIME = timedelta(hours=24)


def _generate_pkce_pair() -> tuple[str, str]:
    """Return a `(code_verifier, code_challenge)` pair using S256.

    The verifier is a high-entropy random string kept server-side; only its
    SHA-256 digest travels in the authorize URL, so intercepting that URL does
    not let an attacker redeem the resulting code.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class LinearAuthService:
    """Drives Linear's OAuth flow and owns the resulting installations."""

    def __init__(
        self,
        settings: Settings,
        token_store: InMemoryLinearTokenStore | FileLinearTokenStore,
        state_store: InMemoryOAuthStateStore,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._tokens = token_store
        self._states = state_store
        self._http = http_client
        self._graphql = LinearGraphQLClient(http_client)
        # Serializes refresh per organization. Linear's refresh tokens are
        # single-use (rotated on each redemption); without this, two
        # coroutines that both see an expired token at once (easily happens
        # with several Linear calls in flight during one review) each send
        # the same refresh_token, and the loser gets rejected with
        # "Invalid refresh token" — forcing a full manual reconnect even
        # though the installation was never actually invalid.
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, organization_id: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(organization_id)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[organization_id] = lock
        return lock

    # -- Authorize ---------------------------------------------------------

    async def build_authorization_request(self, *, actor: str = "app") -> AuthorizationRequest:
        """Build the URL the browser should be sent to, recording its state.

        Args:
            actor: `app` installs AIDA-MATE as an app user (required for agent
                delegation); `user` would act on the authorizing human's behalf.

        Raises:
            LinearError: if OAuth client credentials are not configured.
        """
        self._require_oauth_config()

        state = secrets.token_urlsafe(32)
        verifier, challenge = _generate_pkce_pair()
        await self._states.put(OAuthState(state=state, code_verifier=verifier))

        params = {
            "client_id": self._settings.linear_client_id,
            "redirect_uri": self._settings.linear_redirect_uri,
            "response_type": "code",
            # Linear expects comma-separated scopes.
            "scope": ",".join(self._settings.linear_scope_list),
            "state": state,
            "actor": actor,
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        logger.info("Built Linear authorization request", extra={"actor": actor})
        return AuthorizationRequest(
            authorization_url=f"{LINEAR_AUTHORIZE_URL}?{urlencode(params)}",
            state=state,
        )

    # -- Callback ----------------------------------------------------------

    async def complete_authorization(self, *, code: str, state: str) -> LinearInstallation:
        """Exchange an authorization code for tokens and persist the installation.

        The state is consumed first: an unknown, replayed, or expired state is
        rejected before any credential is sent to Linear.

        Raises:
            LinearError: if state validation or the token exchange fails.
        """
        self._require_oauth_config()

        pending = await self._states.consume(state)
        if pending is None:
            logger.warning("Rejected Linear OAuth callback with unknown or expired state")
            raise LinearError("OAuth state was invalid, already used, or expired")

        token_response = await self._exchange_code(code=code, code_verifier=pending.code_verifier)

        access_token = token_response.get("access_token")
        if not access_token:
            raise LinearError("Linear token response contained no access_token")

        viewer = await fetch_viewer(self._graphql, access_token)

        installation = LinearInstallation(
            organization_id=viewer.organization_id,
            organization_name=viewer.organization_name,
            actor_id=viewer.actor_id,
            access_token=access_token,
            refresh_token=token_response.get("refresh_token"),
            expires_at=self._expiry_from(token_response.get("expires_in")),
            scopes=self._scopes_from(token_response.get("scope")),
        )
        await self._tokens.save(installation)

        logger.info(
            "Linear workspace authorized",
            extra={
                "organization_id": installation.organization_id,
                "organization_name": installation.organization_name,
                "actor_id": installation.actor_id,
                "scopes": installation.scopes,
            },
        )
        return installation

    # -- Token access ------------------------------------------------------

    async def get_access_token(self, organization_id: str | None = None) -> str:
        """Return a currently-valid access token, refreshing if needed.

        Every caller should go through this rather than reading a stored token
        directly, so expiry is handled in exactly one place.

        Raises:
            LinearError: if the workspace is not authorized, or refresh fails.
        """
        installation = await self._resolve_installation(organization_id)
        if not installation.is_expired():
            return installation.access_token.get_secret_value()

        async with self._lock_for(installation.organization_id):
            # Re-resolve under the lock: another coroutine may have already
            # refreshed this installation while we were waiting for it.
            installation = await self._resolve_installation(installation.organization_id)
            if installation.is_expired():
                installation = await self.refresh(installation)
            return installation.access_token.get_secret_value()

    async def refresh(self, installation: LinearInstallation) -> LinearInstallation:
        """Exchange the refresh token for a new access token.

        Raises:
            LinearError: if no refresh token is held or the exchange fails.
        """
        if installation.refresh_token is None:
            raise LinearError(
                f"Linear access token for organization {installation.organization_id} expired "
                "and no refresh token is available; the workspace must reconnect"
            )

        response = await self._post_form(
            LINEAR_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": installation.refresh_token.get_secret_value(),
                "client_id": self._settings.linear_client_id,
                "client_secret": self._settings.linear_client_secret,
            },
            operation="token refresh",
        )

        access_token = response.get("access_token")
        if not access_token:
            raise LinearError("Linear refresh response contained no access_token")

        # `model_copy(update=...)` bypasses validation, so the SecretStr wrapping
        # that the constructor would normally apply has to be explicit here.
        # Without it these fields would silently become plain `str`.
        rotated = response.get("refresh_token")
        refreshed = installation.model_copy(
            update={
                "access_token": SecretStr(access_token),
                # Linear may or may not rotate the refresh token; keep the old one if not.
                "refresh_token": SecretStr(rotated) if rotated else installation.refresh_token,
                "expires_at": self._expiry_from(response.get("expires_in")),
                "updated_at": datetime.now(UTC),
            }
        )
        await self._tokens.save(refreshed)

        logger.info(
            "Refreshed Linear access token",
            extra={"organization_id": refreshed.organization_id},
        )
        return refreshed

    async def revoke(self, organization_id: str) -> None:
        """Revoke a workspace's token and forget the installation.

        The local record is dropped even if the remote call fails — a token we
        can no longer manage should not stay in the store.
        """
        installation = await self._tokens.get(organization_id)
        if installation is None:
            return

        try:
            await self._http.post(
                LINEAR_REVOKE_URL,
                headers={"Authorization": f"Bearer {installation.access_token.get_secret_value()}"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Linear token revocation call failed; dropping local installation anyway",
                extra={"organization_id": organization_id, "error": type(exc).__name__},
            )

        await self._tokens.delete(organization_id)
        logger.info("Revoked Linear installation", extra={"organization_id": organization_id})

    # -- Internals ---------------------------------------------------------

    def _require_oauth_config(self) -> None:
        """Fail clearly when OAuth client credentials are absent."""
        if not self._settings.linear_oauth_configured:
            raise LinearError("Linear OAuth is not configured (LINEAR_CLIENT_ID / LINEAR_CLIENT_SECRET)")

    async def _resolve_installation(self, organization_id: str | None) -> LinearInstallation:
        """Find the installation for an organization, or the sole one if unambiguous."""
        installation = (
            await self._tokens.get(organization_id)
            if organization_id
            else await self._tokens.get_default()
        )
        if installation is None:
            raise LinearError(
                f"No Linear installation for organization {organization_id or '(unspecified)'}; "
                "the workspace must authorize AIDA-MATE first"
            )
        return installation

    async def _exchange_code(self, *, code: str, code_verifier: str) -> dict:
        """Trade an authorization code for tokens."""
        return await self._post_form(
            LINEAR_TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.linear_redirect_uri,
                "client_id": self._settings.linear_client_id,
                "client_secret": self._settings.linear_client_secret,
                "code_verifier": code_verifier,
            },
            operation="token exchange",
        )

    async def _post_form(self, url: str, data: dict[str, str | None], *, operation: str) -> dict:
        """POST a form-encoded body and return the JSON response.

        Error messages carry the operation name and status only. The request
        body holds the client secret and the response holds tokens, so neither
        is ever echoed into an exception or a log line.
        """
        body = {key: value for key, value in data.items() if value is not None}

        try:
            response = await self._http.post(
                url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise LinearError(f"Linear {operation} request failed: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise LinearError(f"Linear {operation} failed with HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise LinearError(f"Linear {operation} returned a non-JSON response") from exc

    @staticmethod
    def _expiry_from(expires_in: object) -> datetime:
        """Convert `expires_in` seconds to an absolute UTC expiry."""
        seconds = expires_in if isinstance(expires_in, int) else None
        lifetime = timedelta(seconds=seconds) if seconds else _DEFAULT_TOKEN_LIFETIME
        return datetime.now(UTC) + lifetime

    @staticmethod
    def _scopes_from(scope: object) -> list[str]:
        """Parse a granted-scope string, tolerating comma or space separation."""
        if not isinstance(scope, str):
            return []
        return [part.strip() for part in scope.replace(",", " ").split() if part.strip()]
