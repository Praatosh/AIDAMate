"""The `X-Api-Key` gate on `/reviews*` and `/scheduled-prompts*` (app/core/api_auth.py).

Security-audit fix: these two routers previously had no authentication of
any kind — see CLAUDE.md for the finding. `client` (tests/conftest.py) sends
a valid key by default so every *other* test file didn't need to change;
these tests instead build their own request without that default, to prove
the vulnerability these endpoints used to have is actually closed.
"""

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from tests.conftest import MANAGEMENT_API_KEY

_PROTECTED_GET_ROUTES = ["/reviews", "/scheduled-prompts"]


def test_reviews_rejects_a_request_with_no_key(client) -> None:
    response = client.get("/reviews", headers={"X-Api-Key": ""})

    assert response.status_code == 401


def test_reviews_rejects_a_request_with_the_wrong_key(client) -> None:
    response = client.get("/reviews", headers={"X-Api-Key": "not-the-real-key"})

    assert response.status_code == 401


def test_reviews_accepts_the_correct_key(client) -> None:
    """`client`'s own default header already proves this on every other test
    in the suite — asserted explicitly here too so this file stands on its
    own as documentation of the working case, not just the rejected ones."""
    response = client.get("/reviews", headers={"X-Api-Key": MANAGEMENT_API_KEY})

    assert response.status_code == 200


def test_scheduled_prompts_rejects_a_request_with_no_key(client) -> None:
    response = client.get("/scheduled-prompts", headers={"X-Api-Key": ""})

    assert response.status_code == 401


def test_scheduled_prompts_write_routes_are_protected_too(client) -> None:
    """Not just the GETs — POST/PATCH/DELETE are exactly what F-1 was about."""
    response = client.post(
        "/scheduled-prompts",
        json={"title": "x", "prompt": "x", "repository": "acme/api", "timezone": "UTC",
              "linear_issue_id": "issue-1", "run_at_time": "09:00"},
        headers={"X-Api-Key": "wrong"},
    )

    assert response.status_code == 401


def test_retry_route_is_protected(client) -> None:
    response = client.post("/reviews/some-id/retry", headers={"X-Api-Key": "wrong"})

    assert response.status_code == 401


def test_unset_management_api_key_rejects_every_request(monkeypatch) -> None:
    """Fail-closed, not fail-open: an operator who never configures
    MANAGEMENT_API_KEY must not end up with these endpoints wide open —
    that would just be the same vulnerability with extra steps. Matches this
    app's own GITHUB_WEBHOOK_SECRET/GITHUB_REPO_ALLOWLIST precedent."""
    monkeypatch.delenv("MANAGEMENT_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(app) as unconfigured_client:
            response = unconfigured_client.get("/reviews", headers={"X-Api-Key": MANAGEMENT_API_KEY})
            assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_merge_confirm_and_comment_delete_pages_are_unaffected(client) -> None:
    """These two routers keep their own token-in-URL auth model and must
    never require the management API key — that would break the "click the
    link Linear posted" flow with no upside, since they already require an
    unguessable token to do anything."""
    merge_confirm = client.get("/reviews/some-token/merge-confirm", headers={"X-Api-Key": ""})
    comment_delete = client.get("/comments/some-token/delete", headers={"X-Api-Key": ""})

    assert merge_confirm.status_code == 200
    assert comment_delete.status_code == 200


def test_scheduled_prompt_form_is_unaffected(client) -> None:
    """The human-facing web form reuses scheduled_prompts.py's creation logic
    as a plain function call, not by re-entering the JSON API's own routing
    — it must keep working with no API key, same reasoning as above."""
    response = client.get("/scheduled-prompts/new", headers={"X-Api-Key": ""})

    assert response.status_code == 200
