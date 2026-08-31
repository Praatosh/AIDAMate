"""Application-level integration: startup, routing, health, error handling.

Exercises the app through its real ASGI stack with the lifespan running, so
these cover composition-root wiring that unit tests never touch.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.errors import AidaMateError


def test_health(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_degraded_without_a_sandbox(
    monkeypatch: pytest.MonkeyPatch, client
) -> None:
    """No `docker` on PATH means the optional agent stage can't run; say so.

    Explicitly forces `shutil.which` to fail regardless of what's actually
    installed on the machine running this test — the `sandbox_configured`
    property does a real PATH lookup, so leaving this to the host's real state
    would make the test's outcome depend on whether Docker happens to be
    installed wherever it runs.
    """
    monkeypatch.setattr("shutil.which", lambda _: None)

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["sandbox"] is False


def test_readiness_reports_ready_with_a_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")

    from app.core.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    try:
        with TestClient(app) as configured_client:
            body = configured_client.get("/ready").json()
    finally:
        get_settings.cache_clear()

    assert body["status"] == "ready"
    assert body["sandbox"] is True


def test_readiness_never_leaks_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/ready`'s response schema is booleans only — no secret should ever
    reach it, regardless of which credential is set. `docker sandbox` itself
    needs no secret (auth is an out-of-band `sbx login`), so this exercises
    the GitHub credential instead.
    """
    monkeypatch.setenv("GITHUB_DEV_TOKEN", "super-secret-github-token")

    from app.core.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    try:
        with TestClient(app) as configured_client:
            raw = configured_client.get("/ready").text
    finally:
        get_settings.cache_clear()

    assert "super-secret-github-token" not in raw


def test_startup_fails_fast_on_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    from app.main import app

    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()

    try:
        with pytest.raises(ValidationError), TestClient(app):
            pass
    finally:
        get_settings.cache_clear()


def test_job_repository_is_wired_at_startup(client) -> None:
    from app.services.job_repository import InMemoryReviewJobRepository

    assert isinstance(client.app.state.job_repository, InMemoryReviewJobRepository)


def test_oauth_routes_report_unconfigured_rather_than_failing(client) -> None:
    """The default fixture supplies no Linear client credentials.

    Install should say "not configured" (503), not 500. Callback validates its
    own inputs before touching config, so a bare call is a 400.
    """
    assert client.get("/auth/linear/install", follow_redirects=False).status_code == 503
    assert client.get("/auth/linear/callback").status_code == 400


def test_linear_auth_service_is_wired_at_startup(client) -> None:
    from app.services.linear_auth_service import LinearAuthService
    from app.services.token_store import InMemoryLinearTokenStore

    assert isinstance(client.app.state.linear_auth_service, LinearAuthService)
    assert isinstance(client.app.state.linear_token_store, InMemoryLinearTokenStore)


def test_unhandled_exception_returns_generic_body(client) -> None:
    """Internal failures must not leak detail to external callers."""

    @client.app.get("/__boom")
    async def boom():
        raise RuntimeError("internal detail that must not escape")

    response = TestClient(client.app, raise_server_exceptions=False).get("/__boom")

    assert response.status_code == 500
    assert "internal detail" not in response.text


def test_domain_errors_return_their_safe_message(client) -> None:
    from app.core.errors import LinkedPullRequestNotFoundError

    @client.app.get("/__domain_error")
    async def domain_error():
        raise LinkedPullRequestNotFoundError("internal: issue 42 had 0 attachments")

    response = TestClient(client.app, raise_server_exceptions=False).get("/__domain_error")

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "linked_pr_not_found"
    assert "attachments" not in body["detail"]


def test_every_domain_error_has_a_code_and_safe_message() -> None:
    """Guards against a new error class inheriting a generic default."""

    def subclasses(cls: type) -> list[type]:
        found = []
        for sub in cls.__subclasses__():
            found.append(sub)
            found.extend(subclasses(sub))
        return found

    for error_cls in subclasses(AidaMateError):
        assert error_cls.code, f"{error_cls.__name__} has no code"
        assert error_cls.user_message, f"{error_cls.__name__} has no user_message"
