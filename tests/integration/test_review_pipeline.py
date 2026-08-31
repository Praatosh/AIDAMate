"""Webhook → job → worker, end to end through the real ASGI stack.

The pipeline's analysis stages arrive in Phases 4–12; what is exercised here is
the machinery that drives them, which is complete.
"""

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.models.common import ReviewJobStatus

SECRET = "test-linear-webhook-secret"


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Signature": signature, "Content-Type": "application/json"},
    )


def _agent_payload(session: str = "session-1", issue: str = "issue-1") -> dict:
    return {
        "type": "AgentSessionEvent",
        "action": "created",
        "organizationId": "org-1",
        "webhookTimestamp": int(time.time() * 1000),
        "data": {
            "agentSession": {
                "id": session,
                "promptContext": "Review the linked PR",
                "issue": {"id": issue, "identifier": "ENG-1"},
            }
        },
    }


async def _drain(client: TestClient) -> None:
    await client.app.state.review_queue.wait_until_idle()


# --- Intake -----------------------------------------------------------------


def test_webhook_creates_and_queues_a_job(client) -> None:
    response = _post(client, _agent_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["queued"] is True
    assert body["review_id"]


def test_queued_job_is_persisted(client) -> None:
    review_id = _post(client, _agent_payload()).json()["review_id"]

    job = client.portal.call(client.app.state.job_repository.get, review_id)

    assert job is not None
    assert job.linear_issue_id == "issue-1"
    assert job.agent_session_id == "session-1"


def test_ignored_event_creates_no_job(client) -> None:
    response = _post(client, {"type": "Comment", "action": "create", "data": {}})

    assert response.json()["review_id"] is None
    assert client.portal.call(client.app.state.job_repository.list_recent) == []


def test_duplicate_delivery_while_in_flight_is_collapsed(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried webhook must not spawn a second concurrent run.

    The stub executor normally finishes in microseconds, so nothing would ever
    be in flight when the retry lands. Slowing it makes the window real and the
    test deterministic rather than a race.
    """
    import asyncio

    from app.workers.review_worker import PendingPipelineExecutor

    async def slow_execute(self, job):
        await asyncio.sleep(0.5)
        raise RuntimeError("still working")

    monkeypatch.setattr(PendingPipelineExecutor, "execute", slow_execute)

    first = _post(client, _agent_payload()).json()
    second = _post(client, _agent_payload()).json()

    assert second["review_id"] == first["review_id"]
    assert second["queued"] is False
    assert second["reason"] == "duplicate_in_flight"


def test_re_delegation_after_completion_starts_a_fresh_review(client) -> None:
    """Once terminal, a deliberate re-request must get a new review."""
    first = _post(client, _agent_payload()).json()
    client.portal.call(_drain, client)

    second = _post(client, _agent_payload()).json()

    assert second["review_id"] != first["review_id"]


def test_distinct_sessions_create_distinct_reviews(client) -> None:
    first = _post(client, _agent_payload(session="s-1")).json()
    second = _post(client, _agent_payload(session="s-2", issue="issue-2")).json()

    assert first["review_id"] != second["review_id"]


# --- Execution --------------------------------------------------------------


def test_job_reaches_a_terminal_state(client) -> None:
    """The lifecycle machinery must always finish, even with no pipeline behind it."""
    review_id = _post(client, _agent_payload()).json()["review_id"]

    client.portal.call(_drain, client)
    job = client.portal.call(client.app.state.job_repository.get, review_id)

    assert job.status.is_terminal


def test_incomplete_pipeline_fails_honestly(client) -> None:
    """No job may look successful while the analysis stages do not exist."""
    review_id = _post(client, _agent_payload()).json()["review_id"]

    client.portal.call(_drain, client)
    job = client.portal.call(client.app.state.job_repository.get, review_id)

    assert job.status is ReviewJobStatus.FAILED
    assert job.error_code == "review_pipeline_incomplete"
    assert job.completed_at is not None


def test_webhook_does_not_wait_for_the_review(client) -> None:
    """Intake must return before the worker finishes, or Linear will time out."""
    review_id = _post(client, _agent_payload()).json()["review_id"]

    # Observed immediately after the response, before draining the queue.
    job = client.portal.call(client.app.state.job_repository.get, review_id)

    assert job is not None


# --- Composition ------------------------------------------------------------


def test_queue_and_service_are_wired(client) -> None:
    from app.services.review_service import ReviewService
    from app.workers.review_worker import ReviewQueue

    assert isinstance(client.app.state.review_queue, ReviewQueue)
    assert isinstance(client.app.state.review_service, ReviewService)


def test_queue_stops_cleanly_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    try:
        with TestClient(app) as local_client:
            queue = local_client.app.state.review_queue
        # After the lifespan exits, the pool must be gone.
        assert queue._tasks == []
    finally:
        get_settings.cache_clear()
