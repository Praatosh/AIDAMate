"""End-to-end OAuth flow through the real ASGI stack."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.services.linear_auth_service import LINEAR_TOKEN_URL
from app.services.linear_service import LINEAR_GRAPHQL_URL


@pytest.fixture
def oauth_client(monkeypatch: pytest.MonkeyPatch):
    """A client whose app has Linear OAuth credentials configured."""
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-abc")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setenv("LINEAR_REDIRECT_URI", "https://aida-mate.example.com/auth/linear/callback")

    from app.core.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


def _viewer_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "viewer": {"id": "app-user-1"},
                "organization": {"id": "org-1", "name": "Acme"},
            }
        },
    )


# --- Install ----------------------------------------------------------------


def test_install_redirects_to_linear(oauth_client) -> None:
    response = oauth_client.get("/auth/linear/install", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://linear.app/oauth/authorize?")


def test_install_can_return_json(oauth_client) -> None:
    response = oauth_client.get("/auth/linear/install?redirect=false")

    assert response.status_code == 200
    params = parse_qs(urlparse(response.json()["authorization_url"]).query)
    assert params["actor"] == ["app"]
    assert "app:assignable" in params["scope"][0].split(",")


def test_install_unavailable_without_oauth_config(client) -> None:
    """The default fixture has no client credentials; say so rather than 500."""
    assert client.get("/auth/linear/install", follow_redirects=False).status_code == 503


# --- Callback ---------------------------------------------------------------


@respx.mock
def test_full_install_flow(oauth_client) -> None:
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 86399,
                "scope": "read,write,app:assignable",
            },
        )
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    state = parse_qs(
        urlparse(oauth_client.get("/auth/linear/install?redirect=false").json()["authorization_url"]).query
    )["state"][0]

    response = oauth_client.get(f"/auth/linear/callback?code=the-code&state={state}")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["organization_id"] == "org-1"
    assert body["actor_id"] == "app-user-1"


@respx.mock
def test_callback_never_returns_a_token(oauth_client) -> None:
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "super-secret-access-token", "expires_in": 3600}
        )
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    state = parse_qs(
        urlparse(oauth_client.get("/auth/linear/install?redirect=false").json()["authorization_url"]).query
    )["state"][0]

    response = oauth_client.get(f"/auth/linear/callback?code=c&state={state}")

    assert "super-secret-access-token" not in response.text


def test_callback_rejects_forged_state(oauth_client) -> None:
    response = oauth_client.get("/auth/linear/callback?code=c&state=forged-state")

    assert response.status_code == 400


def test_callback_reports_declined_consent(oauth_client) -> None:
    response = oauth_client.get("/auth/linear/callback?error=access_denied&error_description=Nope")

    assert response.status_code == 400
    assert "Nope" in response.json()["detail"]


def test_callback_requires_code_and_state(oauth_client) -> None:
    assert oauth_client.get("/auth/linear/callback").status_code == 400
    assert oauth_client.get("/auth/linear/callback?code=only").status_code == 400


# --- Status -----------------------------------------------------------------


def test_status_empty_before_install(oauth_client) -> None:
    body = oauth_client.get("/auth/linear/status").json()

    assert body == {"count": 0, "installations": []}


@respx.mock
def test_status_lists_installation_without_leaking_tokens(oauth_client) -> None:
    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "secret-token-value", "expires_in": 3600, "scope": "read"}
        )
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    state = parse_qs(
        urlparse(oauth_client.get("/auth/linear/install?redirect=false").json()["authorization_url"]).query
    )["state"][0]
    oauth_client.get(f"/auth/linear/callback?code=c&state={state}")

    response = oauth_client.get("/auth/linear/status")
    body = response.json()

    assert body["count"] == 1
    assert body["installations"][0]["organization_id"] == "org-1"
    assert "secret-token-value" not in response.text


# --- Actor discovery feeds the webhook filter -------------------------------


@respx.mock
def test_installed_actor_id_enables_assignment_trigger(oauth_client) -> None:
    """After install, assignment events can be matched without any config."""
    import hashlib
    import hmac
    import json
    import time

    respx.post(LINEAR_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_viewer_response())

    state = parse_qs(
        urlparse(oauth_client.get("/auth/linear/install?redirect=false").json()["authorization_url"]).query
    )["state"][0]
    oauth_client.get(f"/auth/linear/callback?code=c&state={state}")

    payload = {
        "type": "Issue",
        "action": "update",
        "organizationId": "org-1",
        "webhookTimestamp": int(time.time() * 1000),
        # Matches the actor discovered at install, not anything in config.
        "data": {"id": "issue-9", "identifier": "ENG-9", "assigneeId": "app-user-1"},
        "updatedFrom": {"assigneeId": "human"},
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-linear-webhook-secret", body, hashlib.sha256).hexdigest()

    response = oauth_client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Signature": signature, "Content-Type": "application/json"},
    )

    assert response.status_code == 202
    # A review was created, which is only possible if the actor matched.
    assert response.json()["review_id"]
