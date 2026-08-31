"""Gated auto-merge on a Linear issue's Done transition (CLAUDE.md §1a).

Uses the real `InMemoryReviewJobRepository` (simple enough not to need a
fake) plus small recording fakes for GitHub and Linear, mirroring the style
in `tests/unit/test_orchestrator.py`.
"""

import asyncio

import pytest

from app.core.errors import AidaMateError, PullRequestNotMergeableError
from app.models.common import Area, MergeStatus, ReviewJobStatus, RiskLevel, Severity
from app.models.github import PullRequestRef, RepositoryRef
from app.models.linear import ReviewTrigger
from app.models.review import Finding, ReviewJob, ReviewResult
from app.services.auto_merge_service import AutoMergeService
from app.services.job_repository import InMemoryReviewJobRepository

REPO = RepositoryRef(owner="acme", name="api")
REF = PullRequestRef(repository=REPO, number=431, url="https://github.com/acme/api/pull/431")
BASE_URL = "https://aida-mate.example"


class FakeGitHub:
    """Records merge calls; can be scripted to reject the merge."""

    def __init__(self, *, not_mergeable: bool = False) -> None:
        self._not_mergeable = not_mergeable
        self.merged: list[str] = []

    async def merge_pull_request(self, ref: PullRequestRef, *, merge_method: str = "merge") -> None:
        if self._not_mergeable:
            raise PullRequestNotMergeableError(f"{ref.slug} is not mergeable")
        self.merged.append(ref.slug)


class FakeLinear:
    """Records comments that would be posted to Linear."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    async def add_comment(self, issue_id: str, body: str, *, organization_id=None) -> None:
        self.comments.append((issue_id, body))


def _completed_job(
    *,
    linear_issue_id: str = "issue-1",
    risk: RiskLevel = RiskLevel.LOW,
    with_pr: bool = True,
) -> ReviewJob:
    job = ReviewJob(idempotency_key=f"k:{linear_issue_id}", linear_issue_id=linear_issue_id)
    job.pull_request = REF if with_pr else None
    job.result = ReviewResult(
        risk=risk,
        risk_score=10 if risk is RiskLevel.LOW else 80,
        needs_human_review=False,
        labels=[],
        areas=[Area.AUTHENTICATION],
        findings=[
            Finding(category=Area.AUTHENTICATION, severity=Severity.HIGH, description="Token not checked")
        ],
    )
    job.mark_status(ReviewJobStatus.COMPLETED)
    return job


@pytest.fixture
def repo() -> InMemoryReviewJobRepository:
    return InMemoryReviewJobRepository()


def _trigger(issue_id: str = "issue-1") -> ReviewTrigger:
    return ReviewTrigger(source="issue_done", issue_id=issue_id)


# --- LOW risk: merges immediately --------------------------------------------


async def test_low_risk_merges_immediately(repo: InMemoryReviewJobRepository) -> None:
    job = await repo.create(_completed_job(risk=RiskLevel.LOW))
    github = FakeGitHub()
    linear = FakeLinear()
    service = AutoMergeService(repo, github, linear, base_url=BASE_URL)

    await service.handle_issue_done(_trigger())

    assert github.merged == [REF.slug]
    assert linear.comments == []
    stored = await repo.get(job.id)
    assert stored.merge_status is MergeStatus.MERGED


# --- MEDIUM/HIGH risk: confirmation requested --------------------------------


@pytest.mark.parametrize("risk", [RiskLevel.MEDIUM, RiskLevel.HIGH])
async def test_medium_or_high_risk_requests_confirmation(
    repo: InMemoryReviewJobRepository, risk: RiskLevel
) -> None:
    job = await repo.create(_completed_job(risk=risk))
    github = FakeGitHub()
    linear = FakeLinear()
    service = AutoMergeService(repo, github, linear, base_url=BASE_URL)

    await service.handle_issue_done(_trigger())

    assert github.merged == []
    assert len(linear.comments) == 1
    assert linear.comments[0][0] == "issue-1"
    stored = await repo.get(job.id)
    assert stored.merge_status is MergeStatus.PENDING_CONFIRMATION
    assert stored.merge_confirmation_token is not None
    assert f"/reviews/{stored.merge_confirmation_token}/merge-confirm" in linear.comments[0][1]
    # The confirmation link must not be reachable via the job's own (publicly
    # listed) id — security fix, see CLAUDE.md / merge_confirmation.py.
    assert f"/reviews/{job.id}/merge-confirm" not in linear.comments[0][1]


# --- No-ops --------------------------------------------------------------------


async def test_no_completed_review_is_a_no_op(repo: InMemoryReviewJobRepository) -> None:
    github = FakeGitHub()
    linear = FakeLinear()
    service = AutoMergeService(repo, github, linear, base_url=BASE_URL)

    await service.handle_issue_done(_trigger("unknown-issue"))

    assert github.merged == []
    assert linear.comments == []


async def test_completed_review_with_no_pr_is_a_no_op(repo: InMemoryReviewJobRepository) -> None:
    await repo.create(_completed_job(with_pr=False))
    github = FakeGitHub()
    linear = FakeLinear()
    service = AutoMergeService(repo, github, linear, base_url=BASE_URL)

    await service.handle_issue_done(_trigger())

    assert github.merged == []
    assert linear.comments == []


async def test_redelivery_while_merged_does_not_merge_again(repo: InMemoryReviewJobRepository) -> None:
    job = await repo.create(_completed_job(risk=RiskLevel.LOW))
    job.mark_merged()
    await repo.save(job)
    github = FakeGitHub()
    service = AutoMergeService(repo, github, FakeLinear(), base_url=BASE_URL)

    await service.handle_issue_done(_trigger())

    assert github.merged == []


async def test_redelivery_while_pending_does_not_repost(repo: InMemoryReviewJobRepository) -> None:
    job = await repo.create(_completed_job(risk=RiskLevel.HIGH))
    job.mark_merge_pending()
    await repo.save(job)
    linear = FakeLinear()
    service = AutoMergeService(repo, FakeGitHub(), linear, base_url=BASE_URL)

    await service.handle_issue_done(_trigger())

    assert linear.comments == []


async def test_redelivery_after_decline_does_not_repost(repo: InMemoryReviewJobRepository) -> None:
    """Regression: DECLINED wasn't in the 'already decided' guard, so a
    redelivered Done event (Linear retries deliveries) after a human clicked
    'No' would post a fresh confirmation request as if nothing happened."""
    job = await repo.create(_completed_job(risk=RiskLevel.HIGH))
    job.mark_merge_pending()
    job.mark_merge_declined()
    await repo.save(job)
    linear = FakeLinear()
    service = AutoMergeService(repo, FakeGitHub(), linear, base_url=BASE_URL)

    await service.handle_issue_done(_trigger())

    assert linear.comments == []
    stored = await repo.get(job.id)
    assert stored.merge_status is MergeStatus.DECLINED


async def test_redelivery_after_decline_does_not_merge(repo: InMemoryReviewJobRepository) -> None:
    """Same regression, the more serious half: without the DECLINED guard, a
    LOW-risk job's decline (still possible if a human races a confirmation
    page open before the job resolved LOW) followed by a redelivered Done
    event would merge a PR the human never confirmed. Constructed directly
    via mark_merge_declined() since normal LOW-risk jobs never reach
    PENDING_CONFIRMATION in the first place — this proves the guard covers
    the field regardless of how DECLINED was reached."""
    job = await repo.create(_completed_job(risk=RiskLevel.LOW))
    job.mark_merge_pending()
    job.mark_merge_declined()
    await repo.save(job)
    github = FakeGitHub()
    service = AutoMergeService(repo, github, FakeLinear(), base_url=BASE_URL)

    await service.handle_issue_done(_trigger())

    assert github.merged == []
    stored = await repo.get(job.id)
    assert stored.merge_status is MergeStatus.DECLINED


# --- confirm() -----------------------------------------------------------------


async def test_confirm_approved_merges(repo: InMemoryReviewJobRepository) -> None:
    job = await repo.create(_completed_job(risk=RiskLevel.HIGH))
    job.mark_merge_pending()
    await repo.save(job)
    github = FakeGitHub()
    service = AutoMergeService(repo, github, FakeLinear(), base_url=BASE_URL)

    result = await service.confirm(job.merge_confirmation_token, approved=True)

    assert github.merged == [REF.slug]
    assert result.merge_status is MergeStatus.MERGED


async def test_confirm_declined_does_not_merge(repo: InMemoryReviewJobRepository) -> None:
    job = await repo.create(_completed_job(risk=RiskLevel.HIGH))
    job.mark_merge_pending()
    await repo.save(job)
    github = FakeGitHub()
    service = AutoMergeService(repo, github, FakeLinear(), base_url=BASE_URL)

    result = await service.confirm(job.merge_confirmation_token, approved=False)

    assert github.merged == []
    assert result.merge_status is MergeStatus.DECLINED


async def test_confirm_unknown_review_raises(repo: InMemoryReviewJobRepository) -> None:
    service = AutoMergeService(repo, FakeGitHub(), FakeLinear(), base_url=BASE_URL)

    with pytest.raises(AidaMateError):
        await service.confirm("does-not-exist", approved=True)


async def test_confirm_when_not_pending_raises(repo: InMemoryReviewJobRepository) -> None:
    """A token that was minted (so it's a real, once-valid confirmation link)
    but whose job has since moved past PENDING_CONFIRMATION must still raise
    — same "already decided" guard a replayed/bookmarked link should hit."""
    job = await repo.create(_completed_job(risk=RiskLevel.LOW))
    job.mark_merge_pending()
    job.mark_merge_declined()
    await repo.save(job)

    service = AutoMergeService(repo, FakeGitHub(), FakeLinear(), base_url=BASE_URL)

    with pytest.raises(AidaMateError):
        await service.confirm(job.merge_confirmation_token, approved=True)


async def test_confirm_approved_but_not_mergeable_is_recorded_not_raised(
    repo: InMemoryReviewJobRepository,
) -> None:
    job = await repo.create(_completed_job(risk=RiskLevel.HIGH))
    job.mark_merge_pending()
    await repo.save(job)
    service = AutoMergeService(repo, FakeGitHub(not_mergeable=True), FakeLinear(), base_url=BASE_URL)

    result = await service.confirm(job.merge_confirmation_token, approved=True)

    assert result.merge_status is MergeStatus.FAILED
    assert result.merge_error


# --- Concurrency: redelivery/double-click must not race ------------------------


async def test_confirm_and_handle_issue_done_share_the_same_lock(
    repo: InMemoryReviewJobRepository,
) -> None:
    """Regression: `confirm()` previously locked by token while
    `handle_issue_done` locked by job.id — two different keys for the same
    job, so the two entry points only ever serialized against duplicates of
    themselves, never against each other. Proven directly: holding the
    job.id lock externally must block a concurrent `confirm()` call for that
    same job until released."""
    job = await repo.create(_completed_job(risk=RiskLevel.HIGH))
    job.mark_merge_pending()
    await repo.save(job)
    service = AutoMergeService(repo, FakeGitHub(), FakeLinear(), base_url=BASE_URL)

    async with service._lock_for(job.id):
        task = asyncio.create_task(service.confirm(job.merge_confirmation_token, approved=True))
        await asyncio.sleep(0)  # let confirm() run up to (and block on) the lock
        assert not task.done()

    result = await task
    assert result.merge_status is MergeStatus.MERGED


class SlowFakeGitHub:
    """Like `FakeGitHub`, but yields control mid-merge so a concurrent call
    gets a real chance to interleave — proving the per-job lock actually
    serializes rather than merely happening not to overlap."""

    def __init__(self) -> None:
        self.merged: list[str] = []

    async def merge_pull_request(self, ref: PullRequestRef, *, merge_method: str = "merge") -> None:
        await asyncio.sleep(0)
        self.merged.append(ref.slug)


async def test_concurrent_redeliveries_do_not_both_merge(repo: InMemoryReviewJobRepository) -> None:
    """A redelivered Linear webhook racing the original `handle_issue_done`
    call must not both merge — the loser must see the winner's already-MERGED
    status once it gets the lock, and no-op rather than overwrite it."""
    job = await repo.create(_completed_job(risk=RiskLevel.LOW))
    github = SlowFakeGitHub()
    service = AutoMergeService(repo, github, FakeLinear(), base_url=BASE_URL)

    await asyncio.gather(
        service.handle_issue_done(_trigger()),
        service.handle_issue_done(_trigger()),
    )

    assert github.merged == [REF.slug]
    stored = await repo.get(job.id)
    assert stored.merge_status is MergeStatus.MERGED


class ExplodingGitHub:
    """A GitHub client whose merge call raises something other than
    `PullRequestNotMergeableError` — e.g. a transient failure surviving
    `_request`'s own retry, or any other bug in the call chain."""

    async def merge_pull_request(self, ref: PullRequestRef, *, merge_method: str = "merge") -> None:
        raise RuntimeError("GitHub is unreachable")


class ExplodingLinear:
    """A Linear client whose `add_comment` raises unconditionally."""

    async def add_comment(self, issue_id: str, body: str, *, organization_id=None) -> None:
        raise RuntimeError("Linear is unreachable")


async def test_handle_issue_done_never_raises_on_a_github_failure(
    repo: InMemoryReviewJobRepository,
) -> None:
    """Regression: `handle_issue_done`'s own docstring promises it never
    raises, since it's called directly from the webhook handler which must
    always return a 2xx — but nothing enforced that until now. A GitHub
    failure other than "not mergeable" (e.g. a transient error surviving
    retry) must be swallowed, not propagate past this call."""
    await repo.create(_completed_job(risk=RiskLevel.LOW))
    service = AutoMergeService(repo, ExplodingGitHub(), FakeLinear(), base_url=BASE_URL)

    await service.handle_issue_done(_trigger())  # must not raise


async def test_handle_issue_done_never_raises_on_a_linear_failure(
    repo: InMemoryReviewJobRepository,
) -> None:
    """Same guarantee, on the confirmation-request path's Linear call."""
    await repo.create(_completed_job(risk=RiskLevel.HIGH))
    service = AutoMergeService(repo, FakeGitHub(), ExplodingLinear(), base_url=BASE_URL)

    await service.handle_issue_done(_trigger())  # must not raise


async def test_concurrent_confirm_calls_do_not_both_merge(repo: InMemoryReviewJobRepository) -> None:
    """A double-clicked "Yes, merge" (two POSTs for the same review) must not
    both merge — the second call's `confirm` must see PENDING_CONFIRMATION has
    already resolved and raise, not overwrite the winner's MERGED status."""
    job = await repo.create(_completed_job(risk=RiskLevel.HIGH))
    job.mark_merge_pending()
    await repo.save(job)
    github = SlowFakeGitHub()
    service = AutoMergeService(repo, github, FakeLinear(), base_url=BASE_URL)

    results = await asyncio.gather(
        service.confirm(job.merge_confirmation_token, approved=True),
        service.confirm(job.merge_confirmation_token, approved=True),
        return_exceptions=True,
    )

    assert github.merged == [REF.slug]
    outcomes = [r.merge_status if isinstance(r, ReviewJob) else type(r) for r in results]
    assert outcomes.count(MergeStatus.MERGED) + outcomes.count(AidaMateError) == 2
    stored = await repo.get(job.id)
    assert stored.merge_status is MergeStatus.MERGED
