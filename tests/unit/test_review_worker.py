"""Review worker lifecycle, acknowledgement, and failure handling."""

import asyncio

import pytest

from app.core.errors import AidaMateError, LinkedPullRequestNotFoundError
from app.models.common import ReviewJobStatus
from app.models.linear import AgentActivityType
from app.models.review import ReviewJob
from app.services.job_repository import InMemoryReviewJobRepository
from app.workers.review_worker import (
    NO_PR_LINKED_MESSAGE,
    PendingPipelineExecutor,
    ReviewQueue,
    ReviewWorker,
)


class RecordingLinearService:
    """Captures emitted agent activities and comments instead of calling Linear."""

    def __init__(self, fail: bool = False) -> None:
        self.activities: list[tuple[str, AgentActivityType, str]] = []
        self.comments: list[tuple[str, str]] = []
        self._fail = fail

    async def emit_agent_activity(self, session_id, activity_type, body, *, organization_id=None):
        if self._fail:
            raise RuntimeError("Linear is down")
        self.activities.append((session_id, activity_type, body))

    async def add_comment(self, issue_id, body, *, organization_id=None):
        if self._fail:
            raise RuntimeError("Linear is down")
        self.comments.append((issue_id, body))


class SucceedingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, job: ReviewJob) -> None:
        self.calls.append(job.id)


class RaisingExecutor:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def execute(self, job: ReviewJob) -> None:
        raise self._error


class SlowExecutor:
    async def execute(self, job: ReviewJob) -> None:
        await asyncio.sleep(0.05)


@pytest.fixture
def repo() -> InMemoryReviewJobRepository:
    return InMemoryReviewJobRepository()


async def _job(
    repo: InMemoryReviewJobRepository,
    *,
    session: str | None = "session-1",
    trigger_source: str | None = None,
) -> ReviewJob:
    job = ReviewJob(
        idempotency_key=f"session:{session}" if session else "issue:i-1",
        linear_issue_id="issue-1",
        agent_session_id=session,
        organization_id="org-1",
        trigger_source=trigger_source,
    )
    return await repo.create(job)


# --- Happy path -------------------------------------------------------------


async def test_successful_job_reaches_completed(repo) -> None:
    job = await _job(repo)
    executor = SucceedingExecutor()

    await ReviewWorker(repo, executor, RecordingLinearService()).run(job.id)

    stored = await repo.get(job.id)
    assert stored.status is ReviewJobStatus.COMPLETED
    assert executor.calls == [job.id]
    assert stored.duration_ms is not None


# --- The ten-second rule ----------------------------------------------------


async def test_acknowledgement_is_emitted(repo) -> None:
    job = await _job(repo)
    linear = RecordingLinearService()

    await ReviewWorker(repo, SucceedingExecutor(), linear).run(job.id)

    assert linear.activities[0][0] == "session-1"
    assert linear.activities[0][1] is AgentActivityType.THOUGHT


async def test_acknowledgement_precedes_the_analysis(repo) -> None:
    """Linear's 10s clock means the ack cannot wait on slow work."""
    job = await _job(repo)
    order: list[str] = []

    class OrderingExecutor:
        async def execute(self, job: ReviewJob) -> None:
            order.append("execute")

    class OrderingLinear(RecordingLinearService):
        async def emit_agent_activity(self, *args, **kwargs):
            order.append("acknowledge")
            await super().emit_agent_activity(*args, **kwargs)

    await ReviewWorker(repo, OrderingExecutor(), OrderingLinear()).run(job.id)

    assert order == ["acknowledge", "execute"]


async def test_no_acknowledgement_without_a_session(repo) -> None:
    """A plain assignment has no agent session to emit into."""
    job = await _job(repo, session=None)
    linear = RecordingLinearService()

    await ReviewWorker(repo, SucceedingExecutor(), linear).run(job.id)

    assert linear.activities == []


async def test_failed_acknowledgement_does_not_abort_the_review(repo) -> None:
    """Losing an ack is far better than abandoning the review."""
    job = await _job(repo)
    executor = SucceedingExecutor()

    await ReviewWorker(repo, executor, RecordingLinearService(fail=True)).run(job.id)

    assert (await repo.get(job.id)).status is ReviewJobStatus.COMPLETED
    assert executor.calls == [job.id]


# --- Failure handling -------------------------------------------------------


async def test_domain_error_marks_job_failed_with_its_code(repo) -> None:
    job = await _job(repo)

    await ReviewWorker(repo, RaisingExecutor(LinkedPullRequestNotFoundError()), None).run(job.id)

    stored = await repo.get(job.id)
    assert stored.status is ReviewJobStatus.FAILED
    assert stored.error_code == "linked_pr_not_found"
    assert "Link a PR" in stored.error


async def test_failure_is_reported_into_the_session(repo) -> None:
    job = await _job(repo)
    linear = RecordingLinearService()

    await ReviewWorker(repo, RaisingExecutor(LinkedPullRequestNotFoundError()), linear).run(job.id)

    assert linear.activities[-1][1] is AgentActivityType.ERROR


async def test_no_pr_linked_failure_without_session_is_commented_on_the_issue(repo) -> None:
    """A plain assignment/delegate has no session; it gets a direct issue comment instead."""
    job = await _job(repo, session=None, trigger_source="issue_assignment")
    linear = RecordingLinearService()

    await ReviewWorker(repo, RaisingExecutor(LinkedPullRequestNotFoundError()), linear).run(job.id)

    assert linear.activities == []
    assert linear.comments == [("issue-1", NO_PR_LINKED_MESSAGE)]


async def test_other_failure_without_session_falls_back_to_generic_message(repo) -> None:
    job = await _job(repo, session=None, trigger_source="issue_assignment")
    linear = RecordingLinearService()

    await ReviewWorker(repo, RaisingExecutor(RuntimeError("boom")), linear).run(job.id)

    assert linear.comments == [("issue-1", "AIDA-MATE hit an unexpected error.")]


async def test_auto_trigger_failure_without_session_stays_silent(repo) -> None:
    """`issue_auto` casts a broad net; a PR-less issue there is expected, not worth flagging."""
    job = await _job(repo, session=None, trigger_source="issue_auto")
    linear = RecordingLinearService()

    await ReviewWorker(repo, RaisingExecutor(LinkedPullRequestNotFoundError()), linear).run(job.id)

    assert linear.activities == []
    assert linear.comments == []


async def test_unexpected_error_is_reported_generically(repo) -> None:
    """Internal detail could carry paths or secrets; only a generic message escapes."""
    job = await _job(repo)
    secret = "postgres://user:hunter2@db/internal"

    await ReviewWorker(repo, RaisingExecutor(RuntimeError(secret)), None).run(job.id)

    stored = await repo.get(job.id)
    assert stored.status is ReviewJobStatus.FAILED
    assert stored.error_code == "internal_error"
    assert secret not in (stored.error or "")


async def test_worker_never_raises(repo) -> None:
    """An escaped exception would leave the requester waiting forever."""
    job = await _job(repo)

    await ReviewWorker(repo, RaisingExecutor(RuntimeError("boom")), RecordingLinearService(fail=True)).run(
        job.id
    )

    assert (await repo.get(job.id)).status is ReviewJobStatus.FAILED


async def test_missing_job_is_handled(repo) -> None:
    await ReviewWorker(repo, SucceedingExecutor(), None).run("does-not-exist")


async def test_pending_pipeline_executor_always_fails(repo) -> None:
    """The placeholder must never let a job look successful."""
    job = await _job(repo)

    await ReviewWorker(repo, PendingPipelineExecutor(), None).run(job.id)

    stored = await repo.get(job.id)
    assert stored.status is ReviewJobStatus.FAILED
    assert stored.error_code == "review_pipeline_incomplete"


async def test_pending_pipeline_error_is_a_domain_error() -> None:
    with pytest.raises(AidaMateError):
        await PendingPipelineExecutor().execute(
            ReviewJob(idempotency_key="k", linear_issue_id="i")
        )


# --- Queue ------------------------------------------------------------------


async def test_queue_runs_jobs(repo) -> None:
    job = await _job(repo)
    executor = SucceedingExecutor()
    queue = ReviewQueue(ReviewWorker(repo, executor, None), concurrency=1)

    await queue.start()
    try:
        assert await queue.enqueue(job.id) is True
        await queue.wait_until_idle()
    finally:
        await queue.stop()

    assert executor.calls == [job.id]


async def test_queue_rejects_when_full(repo) -> None:
    """Overload must surface as back-pressure, not unbounded memory growth."""
    queue = ReviewQueue(ReviewWorker(repo, SlowExecutor(), None), concurrency=1, maxsize=2)

    # Not started, so nothing drains it.
    assert await queue.enqueue("a") is True
    assert await queue.enqueue("b") is True
    assert await queue.enqueue("c") is False


async def test_queue_survives_a_defective_worker(repo) -> None:
    """One bad job must not kill the pool for every later job."""

    class ExplodingWorker(ReviewWorker):
        def __init__(self, repository):
            super().__init__(repository, SucceedingExecutor(), None)
            self.seen: list[str] = []

        async def run(self, job_id: str) -> None:
            self.seen.append(job_id)
            if job_id == "bad":
                raise RuntimeError("defect in error handling")

    worker = ExplodingWorker(repo)
    queue = ReviewQueue(worker, concurrency=1)

    await queue.start()
    try:
        await queue.enqueue("bad")
        await queue.enqueue("good")
        await queue.wait_until_idle()
    finally:
        await queue.stop()

    assert worker.seen == ["bad", "good"]


async def test_start_is_idempotent(repo) -> None:
    queue = ReviewQueue(ReviewWorker(repo, SucceedingExecutor(), None), concurrency=2)

    await queue.start()
    await queue.start()
    try:
        assert len(queue._tasks) == 2
    finally:
        await queue.stop()


async def test_stop_is_safe_before_start(repo) -> None:
    await ReviewQueue(ReviewWorker(repo, SucceedingExecutor(), None)).stop()
