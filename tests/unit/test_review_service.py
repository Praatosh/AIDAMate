"""Review intake: job creation, dedup, and queueing."""

import pytest

from app.core.errors import AidaMateError
from app.models.common import ReviewJobStatus
from app.models.linear import ReviewTrigger
from app.models.review import build_content_key, build_intake_key
from app.services.job_repository import InMemoryReviewJobRepository
from app.services.review_service import ReviewService


class FakeQueue:
    """Records enqueues without running anything."""

    def __init__(self, accept: bool = True) -> None:
        self.enqueued: list[str] = []
        self._accept = accept

    async def enqueue(self, job_id: str) -> bool:
        if not self._accept:
            return False
        self.enqueued.append(job_id)
        return True


@pytest.fixture
def repo() -> InMemoryReviewJobRepository:
    return InMemoryReviewJobRepository()


def _trigger(**overrides) -> ReviewTrigger:
    values = {
        "source": "agent_session",
        "issue_id": "issue-1",
        "issue_identifier": "ENG-1",
        "agent_session_id": "session-1",
        "organization_id": "org-1",
    }
    values.update(overrides)
    return ReviewTrigger(**values)


# --- Creation ---------------------------------------------------------------


async def test_submit_creates_and_queues_a_job(repo) -> None:
    queue = FakeQueue()

    result = await ReviewService(repo, queue).submit(_trigger())

    assert result.created is True
    assert result.queued is True
    assert queue.enqueued == [result.job.id]


async def test_job_carries_the_trigger_context(repo) -> None:
    result = await ReviewService(repo, FakeQueue()).submit(_trigger())

    job = result.job
    assert job.linear_issue_id == "issue-1"
    assert job.linear_issue_identifier == "ENG-1"
    assert job.agent_session_id == "session-1"
    assert job.organization_id == "org-1"
    assert job.trigger_source == "agent_session"
    assert job.status is ReviewJobStatus.QUEUED


async def test_assignment_trigger_has_no_session(repo) -> None:
    result = await ReviewService(repo, FakeQueue()).submit(
        _trigger(source="issue_assignment", agent_session_id=None)
    )

    assert result.job.agent_session_id is None
    assert result.job.trigger_source == "issue_assignment"


# --- Intake deduplication ---------------------------------------------------


async def test_duplicate_delivery_does_not_start_a_second_run(repo) -> None:
    """The case worth guarding: a retried webhook while work is in flight."""
    queue = FakeQueue()
    service = ReviewService(repo, queue)

    first = await service.submit(_trigger())
    second = await service.submit(_trigger())

    assert second.created is False
    assert second.queued is False
    assert second.job.id == first.job.id
    assert queue.enqueued == [first.job.id]


async def test_re_delegation_after_completion_starts_a_fresh_review(repo) -> None:
    """A deliberate re-request must not silently return the old result."""
    queue = FakeQueue()
    service = ReviewService(repo, queue)

    first = await service.submit(_trigger())
    first.job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(first.job)

    second = await service.submit(_trigger())

    assert second.created is True
    assert second.job.id != first.job.id


async def test_retry_after_failure_starts_a_fresh_review(repo) -> None:
    queue = FakeQueue()
    service = ReviewService(repo, queue)

    first = await service.submit(_trigger())
    first.job.mark_failed("boom", "failed")
    await repo.save(first.job)

    assert (await service.submit(_trigger())).created is True


async def test_distinct_sessions_are_distinct_jobs(repo) -> None:
    service = ReviewService(repo, FakeQueue())

    a = await service.submit(_trigger(agent_session_id="session-1"))
    b = await service.submit(_trigger(agent_session_id="session-2"))

    assert a.job.id != b.job.id


# --- Back-pressure ----------------------------------------------------------


async def test_full_queue_fails_the_job_rather_than_pretending(repo) -> None:
    """The requester must never believe a review is coming when it is not."""
    result = await ReviewService(repo, FakeQueue(accept=False)).submit(_trigger())

    assert result.queued is False
    assert result.job.status is ReviewJobStatus.FAILED
    assert result.job.error_code == "queue_full"


# --- Keys -------------------------------------------------------------------


def test_intake_key_prefers_the_session() -> None:
    """A Linear session is unique per delegation, so it keys intake precisely."""
    assert build_intake_key("issue-1", "session-9") == "session:session-9"


def test_intake_key_falls_back_to_the_issue() -> None:
    assert build_intake_key("issue-1", None) == "issue:issue-1"


# --- Delivery dedup ----------------------------------------------------------
#
# The cheapest of the three guards: a redelivered webhook is dropped before any
# GitHub call, which the content-key check cannot do (it needs the head SHA and
# so must fetch the PR first).


async def test_a_redelivered_webhook_does_not_create_a_second_job(repo) -> None:
    queue = FakeQueue()
    service = ReviewService(repo, queue)
    first = await service.submit(_trigger(delivery_id="delivery-1"))

    second = await service.submit(_trigger(delivery_id="delivery-1"))

    assert second.created is False
    assert second.job.id == first.job.id
    assert queue.enqueued == [first.job.id]


async def test_a_redelivery_is_dropped_even_after_the_first_job_finished(repo) -> None:
    """Intake dedup alone would not catch this: it only collapses in-flight jobs."""
    service = ReviewService(repo, FakeQueue())
    first = await service.submit(_trigger(delivery_id="delivery-1"))
    first.job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(first.job)

    second = await service.submit(_trigger(delivery_id="delivery-1"))

    assert second.created is False


async def test_distinct_deliveries_are_not_confused(repo) -> None:
    service = ReviewService(repo, FakeQueue())
    first = await service.submit(_trigger(delivery_id="delivery-1"))
    first.job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(first.job)

    second = await service.submit(_trigger(delivery_id="delivery-2"))

    assert second.created is True
    assert second.job.id != first.job.id


# --- Retry -------------------------------------------------------------------
#
# The supported way to re-run a review. Re-delegating in Linear deliberately
# cannot force one, because the content check skips an already-reviewed
# revision — so there has to be an explicit path.


async def _failed_job(repo, service) -> str:
    result = await service.submit(_trigger())
    result.job.mark_failed("boom", "it broke")
    await repo.save(result.job)
    return result.job.id


async def test_retry_creates_a_new_attempt(repo) -> None:
    queue = FakeQueue()
    service = ReviewService(repo, queue)
    original_id = await _failed_job(repo, service)

    retried = await service.retry(original_id)

    assert retried.created is True
    assert retried.job.attempt_number == 2
    assert retried.job.previous_review_id == original_id
    assert retried.job.trigger_source == "retry"
    assert retried.job.id in queue.enqueued


async def test_retry_does_not_reuse_the_original_job(repo) -> None:
    """History must be preserved, so the failed attempt stays queryable."""
    service = ReviewService(repo, FakeQueue())
    original_id = await _failed_job(repo, service)

    retried = await service.retry(original_id)

    assert retried.job.id != original_id
    original = await repo.get(original_id)
    assert original.status is ReviewJobStatus.FAILED


async def test_retry_of_an_interrupted_review_is_allowed(repo) -> None:
    service = ReviewService(repo, FakeQueue())
    result = await service.submit(_trigger())
    result.job.mark_interrupted()
    await repo.save(result.job)

    retried = await service.retry(result.job.id)

    assert retried.created is True


async def test_retry_of_a_completed_review_is_refused(repo) -> None:
    service = ReviewService(repo, FakeQueue())
    result = await service.submit(_trigger())
    result.job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(result.job)

    with pytest.raises(AidaMateError, match="COMPLETED"):
        await service.retry(result.job.id)


async def test_retry_of_a_running_review_is_refused(repo) -> None:
    service = ReviewService(repo, FakeQueue())
    result = await service.submit(_trigger())

    with pytest.raises(AidaMateError, match="QUEUED"):
        await service.retry(result.job.id)


async def test_retry_of_an_unknown_review_is_refused(repo) -> None:
    with pytest.raises(AidaMateError, match="No review"):
        await ReviewService(repo, FakeQueue()).retry("does-not-exist")


async def test_repeated_retries_keep_incrementing_the_attempt(repo) -> None:
    service = ReviewService(repo, FakeQueue())
    original_id = await _failed_job(repo, service)

    second = await service.retry(original_id)
    second.job.mark_failed("boom", "again")
    await repo.save(second.job)
    third = await service.retry(second.job.id)

    assert third.job.attempt_number == 3


def test_intake_and_content_keys_are_different_namespaces() -> None:
    """They dedup different things and must never collide."""
    assert build_intake_key("issue-1", None) != build_content_key("issue-1", 1, "sha")


def test_content_key_changes_with_new_commits() -> None:
    assert build_content_key("i", 1, "abc") != build_content_key("i", 1, "def")
