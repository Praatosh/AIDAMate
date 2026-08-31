"""Review inspection and retry endpoints.

Exists for one reason: re-delegating a Linear issue deliberately *cannot*
force a re-review — the content check skips a revision that was already
reviewed — so there has to be an explicit way to say "run that one again".
Before this, the only way to retry was to unassign and reassign in Linear,
which conflated "who owns this issue" with "run exactly one review".

Read-only listing is included because a retry is useless without a way to find
the id to retry.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core.api_auth import require_management_api_key
from app.core.errors import AidaMateError
from app.core.logging import get_logger
from app.models.common import ReviewJobStatus
from app.models.review import ReviewJob

logger = get_logger(__name__)

# Requires a valid X-Api-Key on every route below — see app/core/api_auth.py
# for why this router in particular needed it (security-audit finding).
router = APIRouter(prefix="/reviews", tags=["reviews"], dependencies=[Depends(require_management_api_key)])


class ReviewSummary(BaseModel):
    """One review, as reported over HTTP.

    Identity, status, and verdict only — never tokens, and never the raw
    repository content the agent read.
    """

    id: str
    linear_issue_id: str
    linear_issue_identifier: str | None = None
    github_pr: str | None = None
    head_sha: str | None = None
    status: ReviewJobStatus
    attempt_number: int
    previous_review_id: str | None = None
    trigger_source: str | None = None
    risk: str | None = None
    ai_analysis_ran: bool | None = None
    error_code: str | None = None
    error: str | None = None
    created_at: str
    duration_ms: int | None = None

    @classmethod
    def of(cls, job: ReviewJob) -> "ReviewSummary":
        """Project a `ReviewJob` onto its safe, public shape."""
        return cls(
            id=job.id,
            linear_issue_id=job.linear_issue_id,
            linear_issue_identifier=job.linear_issue_identifier,
            github_pr=job.pull_request.slug if job.pull_request else None,
            head_sha=job.head_sha,
            status=job.status,
            attempt_number=job.attempt_number,
            previous_review_id=job.previous_review_id,
            trigger_source=job.trigger_source,
            risk=job.risk_level.value if job.risk_level else None,
            ai_analysis_ran=job.result.ai_analysis_ran if job.result else None,
            error_code=job.error_code,
            error=job.error,
            created_at=job.created_at.isoformat(),
            duration_ms=job.duration_ms,
        )


class RetryResponse(BaseModel):
    """Result of asking for a review to be run again."""

    retried: bool
    review_id: str
    previous_review_id: str
    attempt_number: int
    queued: bool


@router.get("", response_model=list[ReviewSummary])
async def list_reviews(request: Request, limit: int = 20) -> list[ReviewSummary]:
    """List recent reviews, newest first."""
    jobs = await request.app.state.job_repository.list_recent(limit=limit)
    return [ReviewSummary.of(job) for job in jobs]


@router.get("/{review_id}", response_model=ReviewSummary)
async def get_review(request: Request, review_id: str) -> ReviewSummary:
    """Fetch one review by id."""
    job = await request.app.state.job_repository.get(review_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such review.")
    return ReviewSummary.of(job)


@router.post("/{review_id}/retry", response_model=RetryResponse)
async def retry_review(request: Request, review_id: str) -> RetryResponse:
    """Run a failed or interrupted review again as a new attempt.

    The original job is left untouched so history is preserved; the retry is a
    new job chained to it via `previous_review_id`. Rejected with 409 for a
    review that completed, was skipped, or is still running — none of which
    have anything to retry.
    """
    try:
        result = await request.app.state.review_service.retry(review_id)
    except AidaMateError as exc:
        # Both "unknown id" and "wrong state" arrive here; the message
        # distinguishes them and is already developer-safe.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return RetryResponse(
        retried=True,
        review_id=result.job.id,
        previous_review_id=review_id,
        attempt_number=result.job.attempt_number,
        queued=result.queued,
    )
