"""Background execution of review jobs.

Two pieces:

* `ReviewWorker` — runs one job through its lifecycle, reporting progress into
  the Linear agent session and always reaching a terminal state.
* `ReviewQueue` — a bounded in-process queue with a fixed pool of workers.

Why a queue rather than FastAPI `BackgroundTasks`: a review takes minutes, so
its lifetime must not be tied to the HTTP request that triggered it, and
unbounded concurrency would let a burst of webhooks exhaust sandbox quota and
LLM budget at once. A fixed pool gives predictable resource use; a bounded
queue turns overload into visible back-pressure rather than silent memory
growth. A durable, cross-process queue replaces this in Phase 13.

**The ten-second rule.** Linear marks an agent session unresponsive if no
activity arrives within ten seconds of the `created` event. Acknowledgement is
therefore the very first thing a worker does, before any slow work.
"""

import asyncio
from typing import Protocol

from app.core.errors import AidaMateError, LinkedPullRequestNotFoundError, ReviewPipelineIncompleteError
from app.core.events import (
    ANALYSIS_STARTED,
    REVIEW_COMPLETED,
    REVIEW_FAILED,
)
from app.core.logging import get_logger
from app.models.common import ReviewJobStatus
from app.models.linear import AgentActivityType
from app.models.review import ReviewJob
from app.services.job_repository import InMemoryReviewJobRepository

logger = get_logger(__name__)

ACKNOWLEDGEMENT_TEXT = "Looking at the linked pull request…"

#: Posted verbatim as a Linear comment when AIDA-MATE is explicitly assigned/
#: delegated an issue that has no resolvable GitHub pull request. Distinct
#: from `LinkedPullRequestNotFoundError.user_message` (which stays generic for
#: the API error response) because this is the exact wording asked for the
#: issue-comment surface specifically.
NO_PR_LINKED_MESSAGE = "There is no PR linked here, link and then create an issue."

#: Trigger sources where a "no PR yet" outcome is expected and common, so
#: staying silent (no comment) is correct — `issue_auto` deliberately casts a
#: broad net over every issue touch and most will have no PR at all.
_SILENT_TRIGGER_SOURCES = frozenset({"issue_auto"})


class IReviewExecutor(Protocol):
    """Runs the actual analysis for a job.

    The seam between the lifecycle machinery (complete) and the analysis
    pipeline (built across Phases 4–12). Swapping the implementation requires
    no change to the worker, the queue, or the intake path.
    """

    async def execute(self, job: ReviewJob) -> None: ...


class PendingPipelineExecutor:
    """Placeholder executor used until the analysis pipeline exists.

    Fails loudly and specifically rather than returning a fabricated result.
    Replaced in Phase 4 by the real resolve → fetch → sandbox → analyze chain.
    """

    async def execute(self, job: ReviewJob) -> None:
        """Always raise, so no job can appear to have succeeded."""
        raise ReviewPipelineIncompleteError(
            f"Analysis pipeline not implemented; cannot review issue {job.linear_issue_id}"
        )


class ReviewWorker:
    """Drives one review job from QUEUED to a terminal state."""

    def __init__(
        self,
        repository: InMemoryReviewJobRepository,
        executor: IReviewExecutor,
        linear_service: object | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._linear = linear_service

    async def run(self, job_id: str) -> None:
        """Execute a job, guaranteeing it ends terminal and is reported.

        Never raises: a job that escaped with an exception would leave the
        requester waiting on a review that is not coming.
        """
        job = await self._repository.get(job_id)
        if job is None:
            logger.error("Review job vanished before execution", extra={"review_id": job_id})
            return

        job.mark_status(ReviewJobStatus.PROVISIONING)
        await self._repository.save(job)

        # First, and before anything slow — Linear's ten-second clock.
        await self._acknowledge(job)

        logger.info(ANALYSIS_STARTED, extra={**job.log_context(), "event": ANALYSIS_STARTED})

        try:
            await self._executor.execute(job)
        except asyncio.CancelledError:
            # Queue shutdown (`ReviewQueue.stop()`) cancels every in-flight
            # worker task. `mark_status`/`mark_failed` are never reached in
            # that case, leaving the job stuck at whatever intermediate
            # status it last saved (e.g. ANALYZING) until the next startup's
            # `mark_stale_jobs_interrupted` reconciliation sweep catches it —
            # correct eventually, but stuck-looking in the meantime. Recording
            # INTERRUPTED here immediately makes it visible to the retry path
            # right away. Must re-raise (never swallow a CancelledError) so
            # the task's own cancellation completes and `stop()`'s
            # `asyncio.gather(..., return_exceptions=True)` unwinds cleanly.
            job.mark_interrupted()
            await self._repository.save(job)
            raise
        except AidaMateError as exc:
            await self._fail(job, exc.code, exc.user_message)
            return
        except Exception:
            # Unexpected faults are logged in full but reported generically:
            # an internal message could carry paths, payloads, or secrets.
            logger.exception("Review job failed unexpectedly", extra=job.log_context())
            await self._fail(job, "internal_error", "AIDA-MATE hit an unexpected error.")
            return

        # The executor may have terminated the job itself — SKIPPED, when this
        # PR revision was already reviewed. Stamping COMPLETED over that would
        # erase the distinction between "reviewed" and "deliberately not
        # re-reviewed", and would make the skip invisible to the retry path.
        if job.status.is_terminal:
            await self._repository.save(job)
            return

        job.mark_status(ReviewJobStatus.COMPLETED)
        await self._repository.save(job)
        logger.info(REVIEW_COMPLETED, extra={**job.log_context(), "event": REVIEW_COMPLETED})

    async def _acknowledge(self, job: ReviewJob) -> None:
        """Tell Linear work has started.

        Only meaningful for agent-session triggers; a plain assignment has no
        session to emit into. Failure here is logged but never fatal — losing
        an acknowledgement is far better than abandoning the review.
        """
        if not job.agent_session_id or self._linear is None:
            return

        try:
            await self._linear.emit_agent_activity(
                job.agent_session_id,
                AgentActivityType.THOUGHT,
                ACKNOWLEDGEMENT_TEXT,
                organization_id=job.organization_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to acknowledge Linear agent session; continuing with the review",
                extra={**job.log_context(), "error": type(exc).__name__},
            )

    async def _fail(self, job: ReviewJob, code: str, message: str) -> None:
        """Record a terminal failure and report it back to Linear.

        An agent session gets the activity stream. A plain assignment/delegate
        has no session to emit into, but was still an explicit ask for a
        review — it gets a direct issue comment instead, unless the trigger
        was the broad, best-effort `issue_auto` scan, where a PR-less issue is
        the expected common case rather than something worth flagging.
        """
        job.mark_failed(code, message)
        await self._repository.save(job)
        logger.warning(
            REVIEW_FAILED,
            extra={**job.log_context(), "event": REVIEW_FAILED, "error_code": code},
        )

        if self._linear is None:
            return

        try:
            if job.agent_session_id:
                await self._linear.emit_agent_activity(
                    job.agent_session_id,
                    AgentActivityType.ERROR,
                    message,
                    organization_id=job.organization_id,
                )
            elif job.trigger_source not in _SILENT_TRIGGER_SOURCES:
                body = NO_PR_LINKED_MESSAGE if code == LinkedPullRequestNotFoundError.code else message
                await self._linear.add_comment(
                    job.linear_issue_id, body, organization_id=job.organization_id
                )
        except Exception as exc:
            logger.warning(
                "Failed to report review failure to Linear",
                extra={**job.log_context(), "error": type(exc).__name__},
            )


class ReviewQueue:
    """Bounded in-process job queue with a fixed worker pool."""

    def __init__(self, worker: ReviewWorker, *, concurrency: int = 2, maxsize: int = 100) -> None:
        self._worker = worker
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self._concurrency = concurrency
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    async def start(self) -> None:
        """Spin up the worker pool. Idempotent."""
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._consume(), name=f"review-worker-{index}")
            for index in range(self._concurrency)
        ]
        logger.info("Review queue started", extra={"concurrency": self._concurrency})

    async def stop(self) -> None:
        """Cancel the worker pool and wait for it to unwind."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("Review queue stopped")

    async def enqueue(self, job_id: str) -> bool:
        """Queue a job. Returns False if the queue is full.

        Non-blocking on purpose: this runs on the webhook path, which must
        answer quickly. Blocking here would trade a fast rejection for a slow
        timeout, and Linear would retry the delivery anyway.
        """
        try:
            self._queue.put_nowait(job_id)
        except asyncio.QueueFull:
            logger.error("Review queue is full; rejecting job", extra={"review_id": job_id})
            return False
        return True

    async def wait_until_idle(self) -> None:
        """Block until every queued job has finished. Intended for tests."""
        await self._queue.join()

    @property
    def depth(self) -> int:
        """Jobs waiting or in flight."""
        return self._queue.qsize()

    async def _consume(self) -> None:
        """Pull jobs and run them until cancelled."""
        while True:
            job_id = await self._queue.get()
            try:
                await self._worker.run(job_id)
            except Exception:
                # `run` handles its own errors; this only catches a defect in
                # that handling, which must not kill the worker permanently.
                logger.exception("Review worker loop caught an unhandled error")
            finally:
                self._queue.task_done()
