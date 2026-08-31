"""The merge-confirmation dialog endpoints (CLAUDE.md §1a) — the only HTML page in the app.

GET tests use the default `client` fixture (no GitHub credentials — the dialog
only needs the job repository to render). POST tests that actually merge need
a GitHub credential configured, so they build a fresh `TestClient` with
`GITHUB_DEV_TOKEN` set, mirroring the restart-recovery pattern in
`tests/integration/test_reviews_api.py`.
"""

import asyncio

import httpx
import pytest
import respx

from app.models.common import Area, MergeStatus, ReviewJobStatus, RiskLevel, Severity
from app.models.github import PullRequestRef, RepositoryRef
from app.models.review import Finding, ReviewJob, ReviewResult

REPO = RepositoryRef(owner="acme", name="api")
REF = PullRequestRef(repository=REPO, number=431, url="https://github.com/acme/api/pull/431")


def _pending_job(risk: RiskLevel = RiskLevel.HIGH) -> ReviewJob:
    job = ReviewJob(idempotency_key="k-1", linear_issue_id="issue-1")
    job.pull_request = REF
    job.result = ReviewResult(
        risk=risk,
        risk_score=80,
        needs_human_review=False,
        labels=[],
        areas=[Area.AUTHENTICATION],
        findings=[Finding(category=Area.AUTHENTICATION, severity=Severity.HIGH, description="issue")],
    )
    job.mark_status(ReviewJobStatus.COMPLETED)
    job.mark_merge_pending()
    return job


# --- GET -----------------------------------------------------------------------


def test_get_pending_confirmation_shows_risk_and_areas(client) -> None:
    job = asyncio.run(client.app.state.job_repository.create(_pending_job()))

    response = client.get(f"/reviews/{job.merge_confirmation_token}/merge-confirm")

    assert response.status_code == 200
    assert "HIGH" in response.text
    assert "authentication" in response.text


def test_get_unknown_review_shows_nothing_pending(client) -> None:
    response = client.get("/reviews/does-not-exist/merge-confirm")

    assert response.status_code == 200
    assert "Nothing pending" in response.text


def test_get_already_decided_shows_nothing_pending(client) -> None:
    job = _pending_job()
    job.mark_merged()
    asyncio.run(client.app.state.job_repository.create(job))

    response = client.get(f"/reviews/{job.merge_confirmation_token}/merge-confirm")

    assert "Nothing pending" in response.text


def test_findings_are_html_escaped(client) -> None:
    """A finding's description can originate from LLM output — must never be raw HTML."""
    job = _pending_job()
    job.result.findings[0].description = "<script>alert(1)</script>"
    asyncio.run(client.app.state.job_repository.create(job))

    response = client.get(f"/reviews/{job.merge_confirmation_token}/merge-confirm")

    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_page_sets_a_no_referrer_policy(client) -> None:
    """Security-audit finding (low severity, defense-in-depth): the page's own
    URL contains an unguessable bearer token (see this module's docstring).
    Following the outbound PR link must not leak that URL to GitHub via the
    Referer header — modern browsers' default policy already truncates it,
    but this makes the policy explicit rather than implicit."""
    job = asyncio.run(client.app.state.job_repository.create(_pending_job()))

    response = client.get(f"/reviews/{job.merge_confirmation_token}/merge-confirm")

    assert "name='referrer' content='no-referrer'" in response.text
    assert "rel='noreferrer'" in response.text


def test_review_id_alone_does_not_open_the_confirmation_dialog(client) -> None:
    """Security fix: `job.id` (returned by the unauthenticated GET /reviews
    listing) must not, on its own, work as the confirmation link — only the
    separate `merge_confirmation_token` does."""
    job = asyncio.run(client.app.state.job_repository.create(_pending_job()))
    assert job.id != job.merge_confirmation_token

    response = client.get(f"/reviews/{job.id}/merge-confirm")

    assert "Nothing pending" in response.text


def test_post_without_auto_merge_service_configured_shows_nothing_pending(client) -> None:
    """Default test env has no GitHub credentials, so `auto_merge_service` is None."""
    job = asyncio.run(client.app.state.job_repository.create(_pending_job()))
    assert client.app.state.auto_merge_service is None

    response = client.post(f"/reviews/{job.merge_confirmation_token}/merge-confirm", data={"decision": "no"})

    assert "Nothing pending" in response.text


# --- POST, with a configured GitHub credential ---------------------------------


@pytest.fixture
def merge_capable_client(monkeypatch: pytest.MonkeyPatch):
    """A fresh app instance with GitHub credentials, so `auto_merge_service` exists."""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("GITHUB_DEV_TOKEN", "test-token")
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        get_settings.cache_clear()


def test_post_decision_no_declines_without_calling_github(merge_capable_client) -> None:
    job = asyncio.run(merge_capable_client.app.state.job_repository.create(_pending_job()))

    response = merge_capable_client.post(
        f"/reviews/{job.merge_confirmation_token}/merge-confirm", data={"decision": "no"}
    )

    assert response.status_code == 200
    assert "Left open" in response.text
    stored = asyncio.run(merge_capable_client.app.state.job_repository.get(job.id))
    assert stored.merge_status is MergeStatus.DECLINED


@respx.mock
def test_post_decision_yes_merges(merge_capable_client) -> None:
    job = asyncio.run(merge_capable_client.app.state.job_repository.create(_pending_job()))
    put = respx.put("https://api.github.com/repos/acme/api/pulls/431/merge").mock(
        return_value=httpx.Response(200, json={"merged": True})
    )

    response = merge_capable_client.post(
        f"/reviews/{job.merge_confirmation_token}/merge-confirm", data={"decision": "yes"}
    )

    assert response.status_code == 200
    assert "Merged" in response.text
    assert put.call_count == 1


@respx.mock
def test_post_decision_yes_when_not_mergeable_shows_a_clear_message(merge_capable_client) -> None:
    job = asyncio.run(merge_capable_client.app.state.job_repository.create(_pending_job()))
    respx.put("https://api.github.com/repos/acme/api/pulls/431/merge").mock(
        return_value=httpx.Response(405, json={})
    )

    response = merge_capable_client.post(
        f"/reviews/{job.merge_confirmation_token}/merge-confirm", data={"decision": "yes"}
    )

    assert response.status_code == 200
    assert "Could not merge" in response.text
    assert "Traceback" not in response.text
