"""Shared pytest fixtures.

No test requires a real API key. An autouse fixture injects a dummy
environment, so the suite runs offline on a clean checkout.

That fixture also disables `.env` loading for the whole suite. Once a developer
creates a real `.env` — which Step 1 of local setup requires — `Settings` would
otherwise read it, and the tests would silently depend on whatever happens to
be on that machine: a real credential could turn a "degraded" assertion red,
and a misconfiguration test could stop failing because the value it deleted
from the environment is still present in the file.

The same reasoning applies to `Settings.sandbox_configured`, which does a live
`shutil.which()` PATH lookup rather than reading a credential — so the fixture
also forces that lookup to fail by default, regardless of whether Docker
happens to be installed on whichever machine runs the suite. Individual tests
override this themselves (see `tests/unit/test_config.py`,
`tests/integration/test_app.py`) to exercise the "sandbox available" path.
"""

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import pytest

AIDA_MATE_ACTOR_ID = "aida-mate-actor-id"
WEBHOOK_SECRET = "test-linear-webhook-secret"
#: This suite's fixed dummy value for MANAGEMENT_API_KEY (see app/core/api_auth.py).
#: The `client` fixture below sends it as X-Api-Key by default, so existing
#: tests that never knew this header existed keep working unchanged. Any test
#: that builds its own fresh TestClient (bypassing the `client` fixture) and
#: calls a /reviews* or /scheduled-prompts* route needs to send it too —
#: import this constant rather than hardcoding the string a second time.
MANAGEMENT_API_KEY = "test-management-api-key"


@pytest.fixture(autouse=True)
def dummy_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide a valid dummy configuration for every test.

    Tests exercising misconfiguration delete or override these themselves.
    """
    from app.core.config import Settings

    # Ignore any on-disk .env: the environment injected below is the only
    # configuration a test may see, so results never vary by machine.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MANAGEMENT_API_KEY", MANAGEMENT_API_KEY)
    # "acme/api" is this suite's universal placeholder repo — allowlisted so
    # scheduled-prompt creation tests (which now enforce this, a security fix)
    # don't each need their own override.
    monkeypatch.setenv("GITHUB_REPO_ALLOWLIST", "acme/api")
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("AIDA_MATE_LINEAR_ACTOR_ID", AIDA_MATE_ACTOR_ID)
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    # Deliberately unset: GitHub and Linear credentials. Their absence is a
    # supported (degraded) state and is asserted on.
    for unset in ("GITHUB_APP_ID", "GITHUB_DEV_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(unset, raising=False)

    # `sandbox_configured` checks the real PATH, not an env var — force it
    # absent by default so the suite doesn't vary by host. Tests that need the
    # "sandbox present" branch re-patch this themselves, later in the same
    # test function, which correctly overrides this default.
    monkeypatch.setattr("shutil.which", lambda _: None)

    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    """A TestClient with the app lifespan running.

    Sends X-Api-Key by default (matching MANAGEMENT_API_KEY above), so every
    existing test hitting /reviews* or /scheduled-prompts* keeps working
    without individually knowing this header exists. A test that wants to
    exercise the unauthenticated/wrong-key path removes or overrides the
    header on its own request instead.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, headers={"X-Api-Key": MANAGEMENT_API_KEY}) as test_client:
        yield test_client


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Produce a valid Linear webhook signature for `body`."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def signed_post(client):
    """POST a JSON payload to a webhook route with a valid signature."""

    def _post(path: str, payload: dict[str, Any], *, secret: str = WEBHOOK_SECRET):
        body = json.dumps(payload).encode()
        return client.post(
            path,
            content=body,
            headers={"Linear-Signature": sign(body, secret), "Content-Type": "application/json"},
        )

    return _post


@pytest.fixture
def assignment_payload() -> dict[str, Any]:
    """A Linear webhook payload representing assignment of an issue to AIDA-MATE."""
    return {
        "type": "Issue",
        "action": "update",
        "deliveryId": "delivery-abc-123",
        "data": {
            "id": "issue-uuid-1",
            "identifier": "ENG-123",
            "title": "Harden the login flow",
            "assigneeId": AIDA_MATE_ACTOR_ID,
        },
        "updatedFrom": {"assigneeId": "some-human-user-id"},
    }
