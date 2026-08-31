"""Linear OAuth flow: authorize URL, code exchange, refresh, revoke.

External HTTP is mocked at the transport layer with `respx`, so these exercise
the real service code without a network or a credential.
"""

import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import LinearError
from app.models.linear import LinearInstallation
from app.services.linear_auth_service import (
    LINEAR_REVOKE_URL,
    LINEAR_TOKEN_URL,
    LinearAuthService,
)
from app.services.linear_service import LINEAR_GRAPHQL_URL
from app.services.token_store import InMemoryLinearTokenStore, InMemoryOAuthStateStore


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setenv("LINEAR_REDIRECT_URI", "https://aida-mate.example.com/auth/linear/callback")
    return Settings(_env_file=None)


@pytest.fixture
def token_store() -> InMemoryLinearTokenStore:
    return InMemoryLinearTokenStore()


@pytest.fixture
def service(settings: Settings, token_store: InMemoryLinearTokenStore) -> LinearAuthService:
    return LinearAuthService(
        settings=settings,
        token_store=token_store,
        state_store=InMemoryOAuthStateStore(),
        http_client=httpx.AsyncClient(),
    )


def _viewer_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "viewer": {"id": "app-user-1", "name": "AIDA-MATE"},
                "organization": {"id": "org-1", "name": "Acme", "urlKey": "acme"},
            }
        },
    )


# --- Authorization URL ------------------------------------------------------


async def test_authorization_url_targets_linear(service: LinearAuthService) -> None:
    request = await service.build_authorization_request()
    parsed = urlparse(request.authorization_url)

    assert parsed.scheme == "https"
    assert parsed.netloc == "linear.app"
    assert parsed.path == "/oauth/authorize"


async def test_authorization_url_carries_required_params(service: LinearAuthService) -> None:
    request = await service.build_authorization_request()
    params = parse_qs(urlparse(request.authorization_url).query)

    assert params["client_id"] == ["client-abc"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == ["https://aida-mate.example.com/auth/linear/callback"]
    assert params["state"] == [request.state]


async def test_authorization_uses_app_actor(service: LinearAuthService) -> None:
    """`actor=app` is what makes AIDA-MATE assignable as an agent."""
    params = parse_qs(urlparse((await service.build_authorization_request()).authorization_url).query)

    assert params["actor"] == ["app"]


async def test_scopes_are_comma_separated(service: LinearAuthService) -> None:
    """Linear expects comma separation, not the OAuth-conventional space."""
    params = parse_qs(urlparse((await service.build_authorization_request()).authorization_url).query)

    scope = params["scope"][0]
    assert "," in scope
    assert " " not in scope
    assert "app:assignable" in scope.split(",")


async def test_pkce_challenge_is_s256(service: LinearAuthService) -> None:
    params = parse_qs(urlparse((await service.build_authorization_request()).authorization_url).query)

    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]


async def test_state_is_unpredictable(service: LinearAuthService) -> None:
    """A guessable state would defeat the CSRF protection it exists for."""
    states = {(await service.build_authorization_request()).state for _ in range(20)}

    assert len(states) == 20
    assert all(len(state) >= 32 for state in states)


async def test_authorization_requires_oauth_config(
    monkeypatch: pytest.MonkeyPatch, token_store: InMemoryLinearTokenStore
) -> None:
    monkeypatch.delenv("LINEAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("LINEAR_CLIENT_SECRET", raising=False)
    unconfigured = LinearAuthService(
        settings=Settings(_env_file=None),
        token_store=token_store,
        state_store=InMemoryOAuthStateStore(),
        http_client=httpx.AsyncClient(),
    )

    with pytest.raises(LinearError, match="not configured"):
        await unconfigured.build_authorization_request()


# --- Callback / code exchange ----------------------------------------------


@respx.mock
async def test_successful_authorization_stores_installation(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 86399,
                "scope": "read,write,app:assignable",
                "token_type": "Bearer",
            },
        )
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    request = await service.build_authorization_request()
    installation = await service.complete_authorization(code="auth-code", state=request.state)

    assert installation.organization_id == "org-1"
    assert installation.organization_name == "Acme"
    assert installation.actor_id == "app-user-1"
    assert installation.scopes == ["read", "write", "app:assignable"]
    assert await token_store.get("org-1") is not None


@respx.mock
async def test_actor_id_is_discovered_not_configured(service: LinearAuthService) -> None:
    """The operator never has to look up and paste AIDA-MATE's actor ID."""
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    request = await service.build_authorization_request()
    installation = await service.complete_authorization(code="c", state=request.state)

    assert installation.actor_id == "app-user-1"


@respx.mock
async def test_code_exchange_sends_pkce_verifier_and_secret(service: LinearAuthService) -> None:
    route = respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    request = await service.build_authorization_request()
    challenge_sent = parse_qs(urlparse(request.authorization_url).query)["code_challenge"][0]

    await service.complete_authorization(code="the-code", state=request.state)

    body = parse_qs(route.calls.last.request.content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["the-code"]
    assert body["client_secret"] == ["secret-xyz"]

    # The verifier sent must be the pre-image of the challenge advertised earlier.
    verifier = body["code_verifier"][0]
    recomputed = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert recomputed == challenge_sent


@respx.mock
async def test_token_exchange_is_form_encoded(service: LinearAuthService) -> None:
    """Linear requires application/x-www-form-urlencoded, not JSON."""
    route = respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    request = await service.build_authorization_request()
    await service.complete_authorization(code="c", state=request.state)

    assert route.calls.last.request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )


async def test_unknown_state_is_rejected(service: LinearAuthService) -> None:
    """A forged callback with a state this server never issued must fail."""
    with pytest.raises(LinearError, match="state"):
        await service.complete_authorization(code="c", state="never-issued")


@respx.mock
async def test_state_is_single_use(service: LinearAuthService) -> None:
    """Replaying a captured callback URL must not mint a second token."""
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    request = await service.build_authorization_request()
    await service.complete_authorization(code="c", state=request.state)

    with pytest.raises(LinearError, match="state"):
        await service.complete_authorization(code="c", state=request.state)


@respx.mock
async def test_no_token_stored_when_exchange_fails(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    respx.post(LINEAR_TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "bad_client"}))

    request = await service.build_authorization_request()
    with pytest.raises(LinearError, match="401"):
        await service.complete_authorization(code="c", state=request.state)

    assert await token_store.list_all() == []


@respx.mock
async def test_exchange_failure_does_not_leak_the_client_secret(service: LinearAuthService) -> None:
    """The error surfaces a status, never the credentials in the request body."""
    respx.post(LINEAR_TOKEN_URL).mock(return_value=httpx.Response(400, text="secret-xyz was wrong"))

    request = await service.build_authorization_request()
    with pytest.raises(LinearError) as exc_info:
        await service.complete_authorization(code="c", state=request.state)

    assert "secret-xyz" not in str(exc_info.value)


@respx.mock
async def test_missing_access_token_in_response_is_an_error(service: LinearAuthService) -> None:
    respx.post(LINEAR_TOKEN_URL).mock(return_value=httpx.Response(200, json={"token_type": "Bearer"}))

    request = await service.build_authorization_request()
    with pytest.raises(LinearError, match="access_token"):
        await service.complete_authorization(code="c", state=request.state)


# --- Token lifetime and refresh --------------------------------------------


def _installation(**overrides) -> LinearInstallation:
    values = {
        "organization_id": "org-1",
        "actor_id": "app-user-1",
        "access_token": "old-access",
        "refresh_token": "refresh-1",
        "expires_at": datetime.now(UTC) + timedelta(hours=5),
    }
    values.update(overrides)
    return LinearInstallation(**values)


def test_expiry_uses_leeway() -> None:
    """A token expiring imminently counts as expired, so none is handed out mid-flight."""
    assert _installation(expires_at=datetime.now(UTC) + timedelta(minutes=1)).is_expired() is True
    assert _installation(expires_at=datetime.now(UTC) + timedelta(hours=2)).is_expired() is False


def test_token_without_expiry_never_expires() -> None:
    assert _installation(expires_at=None).is_expired() is False


@respx.mock
async def test_expired_token_is_refreshed_on_access(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    await token_store.save(_installation(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new-access", "expires_in": 86399})
    )

    token = await service.get_access_token("org-1")

    assert token == "new-access"
    assert (await token_store.get("org-1")).access_token.get_secret_value() == "new-access"


@respx.mock
async def test_valid_token_is_not_refreshed(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    await token_store.save(_installation())
    route = respx.post(LINEAR_TOKEN_URL)

    assert await service.get_access_token("org-1") == "old-access"
    assert route.call_count == 0


@respx.mock
async def test_refresh_uses_refresh_grant(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    await token_store.save(_installation())
    route = respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )

    await service.refresh(await token_store.get("org-1"))

    body = parse_qs(route.calls.last.request.content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["refresh_token"] == ["refresh-1"]


@respx.mock
async def test_refresh_token_is_retained_when_not_rotated(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    """Linear may omit a new refresh token; discarding the old one would strand the install."""
    await token_store.save(_installation())
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )

    refreshed = await service.refresh(await token_store.get("org-1"))

    assert refreshed.refresh_token.get_secret_value() == "refresh-1"


async def test_refresh_without_a_refresh_token_is_a_clear_error(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    await token_store.save(_installation(refresh_token=None))

    with pytest.raises(LinearError, match="reconnect"):
        await service.refresh(await token_store.get("org-1"))


@respx.mock
async def test_concurrent_expired_access_does_not_double_refresh(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    """Linear's refresh token is single-use (rotated on redemption). Two
    coroutines that both see the token as expired at once must not both send
    it — the loser would get rejected with "Invalid refresh token" even
    though the installation itself is perfectly valid. Only one HTTP refresh
    call should ever go out, and both callers should get the new token."""
    await token_store.save(_installation(expires_at=datetime.now(UTC) - timedelta(minutes=1)))

    async def _slow_refresh(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)  # yield so the second caller can interleave
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 86399})

    route = respx.post(LINEAR_TOKEN_URL).mock(side_effect=_slow_refresh)

    tokens = await asyncio.gather(
        service.get_access_token("org-1"),
        service.get_access_token("org-1"),
    )

    assert route.call_count == 1
    assert tokens == ["new-access", "new-access"]


async def test_access_token_for_unauthorized_workspace_is_an_error(service: LinearAuthService) -> None:
    with pytest.raises(LinearError, match="authorize"):
        await service.get_access_token("org-unknown")


async def test_default_installation_used_when_org_unspecified(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    await token_store.save(_installation())

    assert await service.get_access_token() == "old-access"


async def test_default_is_ambiguous_with_multiple_workspaces(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    """Guessing which workspace was meant would be worse than failing."""
    await token_store.save(_installation(organization_id="org-1"))
    await token_store.save(_installation(organization_id="org-2"))

    with pytest.raises(LinearError):
        await service.get_access_token()


# --- Revocation -------------------------------------------------------------


@respx.mock
async def test_revoke_removes_the_installation(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    await token_store.save(_installation())
    respx.post(LINEAR_REVOKE_URL).mock(return_value=httpx.Response(200))

    await service.revoke("org-1")

    assert await token_store.get("org-1") is None


@respx.mock
async def test_revoke_drops_local_record_even_if_remote_call_fails(
    service: LinearAuthService, token_store: InMemoryLinearTokenStore
) -> None:
    """A token we can no longer manage should not linger in the store."""
    await token_store.save(_installation())
    respx.post(LINEAR_REVOKE_URL).mock(side_effect=httpx.ConnectError("network down"))

    await service.revoke("org-1")

    assert await token_store.get("org-1") is None


async def test_revoking_unknown_workspace_is_a_no_op(service: LinearAuthService) -> None:
    await service.revoke("org-does-not-exist")


# --- Credential hygiene -----------------------------------------------------


def test_tokens_do_not_appear_in_repr_or_serialization() -> None:
    """SecretStr keeps credentials out of logs, tracebacks, and model dumps."""
    installation = _installation(access_token="super-secret-token")

    assert "super-secret-token" not in repr(installation)
    assert "super-secret-token" not in str(installation.model_dump())
    assert installation.access_token.get_secret_value() == "super-secret-token"
