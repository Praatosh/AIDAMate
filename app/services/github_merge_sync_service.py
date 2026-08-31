"""GitHub merge syncs the linked Linear issue to Done. See CLAUDE.md §1b.

The reverse direction of `app/services/auto_merge_service.py` (CLAUDE.md
§1a): that one reacts to a Linear issue reaching Done by merging the linked
PR; this one reacts to a PR being merged on GitHub by moving the linked
Linear issue to Done. Deliberately a separate service rather than folded
into `AutoMergeService` — the two are opposite-direction reactions to
different webhook sources, and share no state.

    GitHub PR merged into the configured base branch
      -> find the COMPLETED review linking this PR to a Linear issue
        -> none -> no-op
        -> resolve the issue's team's completed-type workflow state
          -> none found -> no-op, logged
          -> found -> move the Linear issue to that state
"""

from app.core.errors import LinearError
from app.core.interfaces import IReviewJobRepository
from app.core.logging import get_logger
from app.models.github import PullRequestRef
from app.services.linear_service import LinearService

logger = get_logger(__name__)


class GitHubMergeSyncService:
    """Moves a Linear issue to Done when its linked PR is merged on GitHub."""

    def __init__(self, repository: IReviewJobRepository, linear: LinearService) -> None:
        self._repository = repository
        self._linear = linear

    async def handle_pull_request_merged(self, ref: PullRequestRef) -> None:
        """React to `ref` having just been merged.

        Never raises: called directly from the GitHub webhook handler, which
        must always return a 2xx regardless of what happens here. Every
        no-op branch below is expected, not an error — an unreviewed PR, an
        issue with no team, or a team with no completed-type state are all
        unremarkable outcomes. A `LinearError` (e.g. a transient API failure,
        or a malformed query — the exact bug live-testing caught the first
        time this ran) is caught around the whole Linear-calling section for
        the same reason: GitHub must still get its 2xx, and Linear being
        unreachable or rejecting a call is not a defect in THIS request.
        """
        job = await self._repository.find_latest_completed_by_pull_request(
            ref.repository.full_name, ref.number
        )
        if job is None:
            logger.info(
                "No completed review links this PR to a Linear issue; nothing to sync",
                extra={"github_pr": ref.slug},
            )
            return

        try:
            issue = await self._linear.get_issue(job.linear_issue_id, organization_id=job.organization_id)
            if issue.team_id is None:
                logger.warning(
                    "Linear issue has no team; cannot resolve a Done state",
                    extra={
                        "review_id": job.id,
                        "linear_issue_id": job.linear_issue_id,
                        "github_pr": ref.slug,
                    },
                )
                return

            done_state_id = await self._linear.find_done_state_id(
                issue.team_id, organization_id=job.organization_id
            )
            if done_state_id is None:
                logger.warning(
                    "No completed-type workflow state found for this team; cannot sync to Done",
                    extra={
                        "review_id": job.id,
                        "linear_issue_id": job.linear_issue_id,
                        "github_pr": ref.slug,
                    },
                )
                return

            await self._linear.update_issue_state(
                job.linear_issue_id, done_state_id, organization_id=job.organization_id
            )
        except LinearError as exc:
            logger.warning(
                "Could not sync merged PR to Linear Done",
                extra={"review_id": job.id, "github_pr": ref.slug, "error": str(exc)},
            )
            return

        logger.info(
            "Synced merged PR to Linear Done",
            extra={"review_id": job.id, "linear_issue_id": job.linear_issue_id, "github_pr": ref.slug},
        )
