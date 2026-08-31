"""Review listing and retry endpoints, through the real ASGI stack.

The retry endpoint is what makes a Linear assignment mean "AIDA-MATE owns this
issue" rather than "run exactly one review": now that the content check
enforces one review per PR revision, unassign/reassign can no longer force a
re-run, so this is the supported path.

Scope here is wiring and error mapping — that the routes exist, reach the
composition root's repository and service, and translate domain errors into
the right status codes. The retry *rules* (attempt numbering, which states are
retryable) are unit-tested against the service in `test_review_service.py`,
where they can be exercised without an HTTP round trip.
"""

import pytest

from tests.conftest import MANAGEMENT_API_KEY


def test_listing_is_empty_on_a_fresh_app(client) -> None:
    response = client.get("/reviews")

    assert response.status_code == 200
    assert response.json() == []


def test_listing_reaches_the_composition_roots_repository(client) -> None:
    """A wiring check: the route must read app.state, not construct its own store."""
    assert client.app.state.job_repository is not None
    assert client.get("/reviews").status_code == 200


def test_listing_accepts_a_limit(client) -> None:
    assert client.get("/reviews", params={"limit": 5}).status_code == 200


def test_fetching_an_unknown_review_is_404(client) -> None:
    response = client.get("/reviews/does-not-exist")

    assert response.status_code == 404


def test_retrying_an_unknown_review_is_409(client) -> None:
    """Domain refusals surface as 409, not as an unhandled 500."""
    response = client.post("/reviews/does-not-exist/retry")

    assert response.status_code == 409
    assert "No review" in response.json()["detail"]


def test_retry_failure_detail_carries_no_stack_trace(client) -> None:
    detail = client.post("/reviews/does-not-exist/retry").json()["detail"]

    assert "Traceback" not in detail
    assert "File \"" not in detail


# --- Restart recovery, through the real lifespan ------------------------------
#
# This is the scenario increment 3 exists for: a process dies mid-review, and
# the review must become visible and retryable afterward rather than vanishing
# (in-memory) or sitting in ANALYZING forever (persisted but unrecovered).


def test_a_job_interrupted_by_a_restart_becomes_retryable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app
    from app.models.common import ReviewJobStatus
    from app.models.review import ReviewJob

    db_path = tmp_path / "reviews.sqlite3"
    monkeypatch.setenv("REVIEW_STORE_PATH", str(db_path))
    get_settings.cache_clear()

    try:
        # First "process": a review starts but the app goes down before it
        # finishes — simulated directly, since actually killing the worker
        # mid-flight isn't something a test can trigger deterministically.
        with TestClient(app) as first_run:
            job = ReviewJob(idempotency_key="session:s-1", linear_issue_id="issue-1")
            job.mark_status(ReviewJobStatus.ANALYZING)
            import asyncio

            asyncio.run(first_run.app.state.job_repository.create(job))

        # Second "process": startup must find and recover it.
        with TestClient(app, headers={"X-Api-Key": MANAGEMENT_API_KEY}) as second_run:
            body = second_run.get(f"/reviews/{job.id}").json()

        assert body["status"] == ReviewJobStatus.INTERRUPTED.value
    finally:
        get_settings.cache_clear()


def test_review_state_survives_a_restart(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the point: a *finished* review is not just recoverable
    but genuinely still there, unlike with in-memory storage."""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app
    from app.models.common import ReviewJobStatus
    from app.models.review import ReviewJob

    db_path = tmp_path / "reviews.sqlite3"
    monkeypatch.setenv("REVIEW_STORE_PATH", str(db_path))
    get_settings.cache_clear()

    try:
        with TestClient(app) as first_run:
            job = ReviewJob(idempotency_key="session:s-1", linear_issue_id="issue-1")
            job.mark_status(ReviewJobStatus.COMPLETED)
            import asyncio

            asyncio.run(first_run.app.state.job_repository.create(job))

        with TestClient(app, headers={"X-Api-Key": MANAGEMENT_API_KEY}) as second_run:
            body = second_run.get(f"/reviews/{job.id}").json()

        assert body["status"] == ReviewJobStatus.COMPLETED.value
    finally:
        get_settings.cache_clear()
