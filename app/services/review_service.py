"""Review job intake.

Turns a validated `ReviewTrigger` into a persisted `ReviewJob` and hands it to
the queue. Deliberately does *not* run the review — that belongs to
`app/workers/review_worker.py`, so the webhook path stays fast and the work
survives the request that started it.

Three independent guards stop duplicate work, at increasing cost:

1. **Delivery** (`delivery_id`) — a redelivered webhook is dropped here, before
   a single GitHub call is made.
2. **Intake** (`idempotency_key`) — a second trigger for an issue already being
   reviewed collapses onto the in-flight job.
3. **Content** (`content_key`, enforced in `agents/orchestrator.py`) — an
   automatic trigger for a PR revision already reviewed stops before the
   sandbox and LLM. Explicit assignment/delegation always runs a fresh review.

Only the third can know the head SHA, which is why it is also the only one that
has to pay for a PR fetch first.
"""

from app.core.errors import AidaMateError
from app.core.events import REVIEW_CREATED, REVIEW_SKIPPED_DUPLICATE
from app.core.logging import get_logger
from app.models.linear import ReviewTrigger
from app.models.review import ReviewJob, build_intake_key
from app.services.job_repository import InMemoryReviewJobRepository
from app.workers.review_worker import ReviewQueue

logger = get_logger(__name__)


class ReviewIntakeResult:
    """Outcome of offering a trigger to the intake path."""

    def __init__(self, job: ReviewJob, created: bool, queued: bool) -> None:
        self.job = job
        self.created = created
        self.queued = queued


class ReviewService:
    """Creates review jobs from triggers and enqueues them."""

    def __init__(self, repository: InMemoryReviewJobRepository, queue: ReviewQueue) -> None:
        self._repository = repository
        self._queue = queue

    async def submit(self, trigger: ReviewTrigger) -> ReviewIntakeResult:
        """Create a job for `trigger` and queue it, unless it is a duplicate."""
        if trigger.delivery_id:
            seen = await self._repository.find_by_delivery_id(trigger.delivery_id)
            if seen is not None:
                logger.info(
                    REVIEW_SKIPPED_DUPLICATE,
                    extra={
                        **seen.log_context(),
                        "event": REVIEW_SKIPPED_DUPLICATE,
                        "reason": "redelivered_webhook",
                        "delivery_id": trigger.delivery_id,
                    },
                )
                return ReviewIntakeResult(job=seen, created=False, queued=False)

        job = ReviewJob(
            idempotency_key=build_intake_key(trigger.issue_id, trigger.agent_session_id),
            linear_issue_id=trigger.issue_id,
            linear_issue_identifier=trigger.issue_identifier,
            agent_session_id=trigger.agent_session_id,
            organization_id=trigger.organization_id,
            trigger_source=trigger.source,
            delivery_id=trigger.delivery_id,
        )

        stored, created = await self._repository.create_or_get(job)

        if not created:
            logger.info(
                REVIEW_SKIPPED_DUPLICATE,
                extra={
                    **stored.log_context(),
                    "event": REVIEW_SKIPPED_DUPLICATE,
                    "reason": "already_in_flight",
                },
            )
            return ReviewIntakeResult(job=stored, created=False, queued=False)

        return await self._enqueue(stored)

    async def retry(self, review_id: str) -> ReviewIntakeResult:
        """Start a fresh attempt at a review that failed or was interrupted.

        This is the supported way to re-run a failed or interrupted review.
        Explicitly assigning/delegating an issue is also a fresh review request;
        this endpoint is useful when that assignment-triggered attempt needs
        recovery.

        The new job keeps the original's identity (same issue, chained through
        `previous_review_id`) and increments `attempt_number`, which is also
        what tells the orchestrator to bypass the already-reviewed check. The
        pipeline re-resolves the PR from scratch, so if new commits landed since
        the failure, the retry naturally reviews the current revision rather
        than a stale one.

        Raises:
            AidaMateError: if the job is unknown, or is in a state where a retry
                is meaningless (completed, skipped, or still running).
        """
        original = await self._repository.get(review_id)
        if original is None:
            raise AidaMateError(f"No review with id {review_id}")

        if not original.status.is_retryable:
            raise AidaMateError(
                f"Review {review_id} is {original.status.value}; only failed or interrupted "
                "reviews can be retried."
            )

        attempt = original.attempt_number + 1
        job = ReviewJob(
            # Attempt-scoped so it can never collide with the issue-level key a
            # webhook would generate, nor with an earlier attempt's.
            idempotency_key=f"retry:{original.id}:{attempt}",
            linear_issue_id=original.linear_issue_id,
            linear_issue_identifier=original.linear_issue_identifier,
            agent_session_id=original.agent_session_id,
            organization_id=original.organization_id,
            trigger_source="retry",
            attempt_number=attempt,
            previous_review_id=original.id,
        )

        stored, created = await self._repository.create_or_get(job)
        if not created:
            return ReviewIntakeResult(job=stored, created=False, queued=False)

        return await self._enqueue(stored)

    async def _enqueue(self, job: ReviewJob) -> ReviewIntakeResult:
        """Queue a freshly created job, recording back-pressure as a failure."""
        queued = await self._queue.enqueue(job.id)
        if not queued:
            # A full queue is back-pressure, not a bug — but the requester must
            # not be left believing a review is coming.
            job.mark_failed("queue_full", "AIDA-MATE is at capacity; please try again shortly.")
            await self._repository.save(job)

        logger.info(
            REVIEW_CREATED,
            extra={**job.log_context(), "event": REVIEW_CREATED, "queued": queued},
        )
        return ReviewIntakeResult(job=job, created=True, queued=queued)
