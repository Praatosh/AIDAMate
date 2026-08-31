"""Gated auto-merge on a Linear issue's Done transition. See CLAUDE.md §1a.

Deliberately outside the review pipeline: this never runs a sandbox or an
LLM, and never re-classifies risk — it only acts on a `ReviewResult` already
published by `ReviewOrchestrator`. It never creates a `ReviewJob` and never
touches `ReviewQueue`/`ReviewWorker`/`ReviewOrchestrator`.

    Linear issue -> completed-type state (human action)
      -> find latest COMPLETED review + its linked PR
        -> risk == LOW          -> merge immediately, no confirmation
        -> risk == MEDIUM/HIGH  -> Linear comment with a confirmation link
                                    -> human clicks "Yes, merge" -> merge
                                    -> "No" / no answer -> nothing happens

This is the one narrow, explicitly-opt-in exception to "AIDA-MATE never
writes code" — gated behind `Settings.auto_merge_on_done_enabled` (default
off) and triggered only by a human's Linear state change, never by the LLM
or any agent tool.
"""

import asyncio

from app.core.errors import AidaMateError, PullRequestNotMergeableError
from app.core.interfaces import IGitHubClient, IReviewJobRepository
from app.core.logging import get_logger
from app.models.common import MergeStatus, RiskLevel
from app.models.linear import ReviewTrigger
from app.models.review import Finding, ReviewJob
from app.services.linear_service import LinearService

logger = get_logger(__name__)


def _render_confirmation_comment(job: ReviewJob, url: str) -> str:
    """Build the Linear comment markdown for a MEDIUM/HIGH confirmation request."""
    result = job.result
    assert result is not None  # only called once a COMPLETED review is confirmed present

    areas = ", ".join(sorted(area.value for area in result.areas)) or "none detected"
    findings_lines = [_format_finding(finding) for finding in result.findings] or [
        "No specific findings recorded."
    ]

    pr = job.pull_request
    pr_line = f"Pull request: {pr.slug} ({pr.url})" if pr else "Pull request: unknown"

    return "\n".join(
        [
            f"**AIDA-MATE: merge confirmation needed — risk {result.risk.value}**",
            "",
            pr_line,
            f"Affected areas: {areas}",
            "",
            "Findings:",
            *[f"- {line}" for line in findings_lines],
            "",
            "Have you re-checked the code related to this? If not, go check once more before merging.",
            "",
            f"Confirm here: {url}",
        ]
    )


def _format_finding(finding: Finding) -> str:
    return f"[{finding.category.value}/{finding.severity.value}] {finding.description}"


class AutoMergeService:
    """Gated merge action for a Linear issue's Done transition."""

    def __init__(
        self,
        repository: IReviewJobRepository,
        github: IGitHubClient,
        linear: LinearService,
        *,
        base_url: str,
    ) -> None:
        self._repository = repository
        self._github = github
        self._linear = linear
        self._base_url = base_url
        # Serializes the read-decide-write sequence per job. Without this, a
        # redelivered Linear webhook (or a double-clicked confirmation) can
        # race two calls through the same "not already merged" check before
        # either writes back — the loser's `merge_pull_request` then 405s
        # (already merged) and overwrites the winner's correctly-recorded
        # MERGED status with FAILED. Keyed by job id, not a single global
        # lock, so unrelated jobs never block each other.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, job_id: str) -> asyncio.Lock:
        lock = self._locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[job_id] = lock
        return lock

    async def handle_issue_done(self, trigger: ReviewTrigger) -> None:
        """React to a Linear issue landing on a completed-type workflow state.

        Never raises: called directly from the webhook handler, which must
        always return a 2xx to Linear regardless of what happens here — the
        same contract `GitHubMergeSyncService.handle_pull_request_merged`
        documents and enforces for its own, simpler, single-service case
        (CLAUDE.md §8). This one spans both the job repository and two
        external clients (GitHub, Linear), any of which can raise (a
        transient failure surviving retry, a SQLite error, a GitHub error
        other than "not mergeable"), so the guard wraps the whole
        decide-and-act sequence with a catch-all rather than one client's
        calls specifically — found by code audit: this method's own
        docstring claimed "never raises" while nothing here actually
        enforced it, unlike its GitHub-merge-sync mirror.
        """
        try:
            await self._handle_issue_done(trigger)
        except Exception:
            logger.exception(
                "Auto-merge handling failed for this issue-done event",
                extra={"linear_issue_id": trigger.issue_id},
            )

    async def _handle_issue_done(self, trigger: ReviewTrigger) -> None:
        job = await self._repository.find_latest_completed_by_linear_issue_id(trigger.issue_id)
        if job is None or job.pull_request is None or job.result is None:
            logger.info(
                "No completed review with a linked PR for this issue; nothing to merge",
                extra={"linear_issue_id": trigger.issue_id},
            )
            return

        async with self._lock_for(job.id):
            # Re-read under the lock: another call may have already resolved
            # this job while we were waiting for it.
            current = await self._repository.get(job.id)
            job = current if current is not None else job

            if job.merge_status in (MergeStatus.MERGED, MergeStatus.PENDING_CONFIRMATION):
                logger.info(
                    "Merge already decided or pending; ignoring redelivery",
                    extra={"review_id": job.id, "merge_status": job.merge_status.value},
                )
                return

            if job.result.risk is RiskLevel.LOW:
                await self._merge(job)
            else:
                await self._request_confirmation(job)

    async def _merge(self, job: ReviewJob) -> None:
        """Merge `job`'s linked pull request, recording the outcome either way."""
        try:
            await self._github.merge_pull_request(job.pull_request)
        except PullRequestNotMergeableError as exc:
            job.mark_merge_failed(exc.user_message)
            await self._repository.save(job)
            logger.warning(
                "Merge failed: not mergeable", extra={"review_id": job.id, "github_pr": job.pull_request.slug}
            )
            return

        job.mark_merged()
        await self._repository.save(job)
        logger.info("Merge completed", extra={"review_id": job.id, "github_pr": job.pull_request.slug})

    async def _request_confirmation(self, job: ReviewJob) -> None:
        """Park `job` pending human confirmation and post the link to Linear."""
        job.mark_merge_pending()
        await self._repository.save(job)

        # Keyed by `merge_confirmation_token`, not `job.id` — `id` is also
        # returned by the unauthenticated GET /reviews listing, so it isn't
        # actually secret. Security-audit finding, fixed here.
        url = f"{self._base_url}/reviews/{job.merge_confirmation_token}/merge-confirm"
        body = _render_confirmation_comment(job, url)
        await self._linear.add_comment(job.linear_issue_id, body, organization_id=job.organization_id)
        logger.info("Posted merge confirmation request to Linear", extra={"review_id": job.id})

    async def confirm(self, token: str, *, approved: bool) -> ReviewJob:
        """Resolve a pending merge confirmation, identified by its token.

        `token` is `merge_confirmation_token`, not `job.id` — see
        `_request_confirmation`'s comment above.

        Raises:
            AidaMateError: unknown token, or no confirmation is pending for it
                (already decided, or never was).
        """
        # Locked by the token itself rather than a job id resolved from it:
        # the token is exactly the value two concurrent calls (e.g. a
        # double-clicked "Yes, merge") would both carry, so it serializes the
        # same race `_lock_for` was already built to guard against.
        async with self._lock_for(token):
            job = await self._repository.find_by_merge_confirmation_token(token)
            if job is None or job.merge_status is not MergeStatus.PENDING_CONFIRMATION:
                raise AidaMateError("This merge confirmation is unknown or already decided.")

            if not approved:
                job.mark_merge_declined()
                await self._repository.save(job)
                logger.info("Merge declined", extra={"review_id": job.id})
                return job

            await self._merge(job)
            return await self._repository.find_by_merge_confirmation_token(token)
