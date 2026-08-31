"""In-memory review job repository, focused on idempotency."""

import asyncio

from app.models.common import ReviewJobStatus
from app.models.github import PullRequestRef, RepositoryRef
from app.models.review import ReviewJob
from app.services.job_repository import InMemoryReviewJobRepository


def _job(key: str = "issue-1:42:abc") -> ReviewJob:
    return ReviewJob(idempotency_key=key, linear_issue_id="issue-1")


async def test_create_then_get() -> None:
    repo = InMemoryReviewJobRepository()
    created = await repo.create(_job())

    assert await repo.get(created.id) is created


async def test_get_unknown_id_returns_none() -> None:
    assert await InMemoryReviewJobRepository().get("nope") is None


async def test_duplicate_key_returns_the_original_job() -> None:
    """A redelivered webhook must not create a second job."""
    repo = InMemoryReviewJobRepository()
    first = await repo.create(_job())
    second = await repo.create(_job())

    assert second.id == first.id
    assert len(await repo.list_recent()) == 1


async def test_distinct_keys_create_distinct_jobs() -> None:
    repo = InMemoryReviewJobRepository()
    await repo.create(_job("issue-1:42:abc"))
    await repo.create(_job("issue-1:42:def"))

    assert len(await repo.list_recent()) == 2


async def test_concurrent_creates_with_same_key_yield_one_job() -> None:
    """Concurrent deliveries must not both win the idempotency check."""
    repo = InMemoryReviewJobRepository()

    results = await asyncio.gather(*(repo.create(_job()) for _ in range(10)))

    assert len({job.id for job in results}) == 1
    assert len(await repo.list_recent()) == 1


async def test_lookup_by_idempotency_key() -> None:
    repo = InMemoryReviewJobRepository()
    created = await repo.create(_job("k-1"))

    assert (await repo.get_by_idempotency_key("k-1")).id == created.id
    assert await repo.get_by_idempotency_key("k-2") is None


async def test_save_persists_mutations() -> None:
    repo = InMemoryReviewJobRepository()
    job = await repo.create(_job())

    job.mark_status(ReviewJobStatus.ANALYZING)
    await repo.save(job)

    assert (await repo.get(job.id)).status is ReviewJobStatus.ANALYZING


async def test_list_recent_is_newest_first_and_limited() -> None:
    repo = InMemoryReviewJobRepository()
    for i in range(5):
        await repo.create(_job(f"key-{i}"))

    recent = await repo.list_recent(limit=3)

    assert len(recent) == 3
    assert recent == sorted(recent, key=lambda j: j.created_at, reverse=True)


# --- find_latest_completed_by_linear_issue_id (CLAUDE.md §1a) ---------------


async def _completed_job(key: str, linear_issue_id: str) -> ReviewJob:
    return ReviewJob(idempotency_key=key, linear_issue_id=linear_issue_id, status=ReviewJobStatus.COMPLETED)


async def test_finds_the_completed_review_for_an_issue() -> None:
    repo = InMemoryReviewJobRepository()
    await repo.create(await _completed_job("k-1", "issue-1"))

    found = await repo.find_latest_completed_by_linear_issue_id("issue-1")

    assert found is not None
    assert found.linear_issue_id == "issue-1"


async def test_ignores_non_completed_jobs_for_the_issue() -> None:
    repo = InMemoryReviewJobRepository()
    await repo.create(_job("k-1"))  # QUEUED, linear_issue_id="issue-1"

    assert await repo.find_latest_completed_by_linear_issue_id("issue-1") is None


async def test_ignores_completed_jobs_for_other_issues() -> None:
    repo = InMemoryReviewJobRepository()
    await repo.create(await _completed_job("k-1", "issue-2"))

    assert await repo.find_latest_completed_by_linear_issue_id("issue-1") is None


async def test_picks_the_most_recent_completed_job_for_the_issue() -> None:
    repo = InMemoryReviewJobRepository()
    older = await repo.create(await _completed_job("k-1", "issue-1"))
    newer = await repo.create(await _completed_job("k-2", "issue-1"))
    newer.created_at = older.created_at.replace(year=older.created_at.year + 1)
    await repo.save(newer)

    found = await repo.find_latest_completed_by_linear_issue_id("issue-1")

    assert found.id == newer.id


# --- find_latest_completed_by_pull_request (CLAUDE.md §1b) -------------------

_REF = PullRequestRef(
    repository=RepositoryRef(owner="acme", name="api"), number=431, url="https://github.com/acme/api/pull/431"
)


async def _completed_pr_job(key: str, ref: PullRequestRef) -> ReviewJob:
    job = ReviewJob(idempotency_key=key, linear_issue_id="issue-1", status=ReviewJobStatus.COMPLETED)
    job.pull_request = ref
    return job


async def test_finds_the_completed_review_for_a_pull_request() -> None:
    repo = InMemoryReviewJobRepository()
    await repo.create(await _completed_pr_job("k-1", _REF))

    found = await repo.find_latest_completed_by_pull_request("acme/api", 431)

    assert found is not None
    assert found.pull_request == _REF


async def test_pull_request_lookup_ignores_non_completed_jobs() -> None:
    repo = InMemoryReviewJobRepository()
    job = _job("k-1")
    job.pull_request = _REF
    await repo.create(job)  # QUEUED

    assert await repo.find_latest_completed_by_pull_request("acme/api", 431) is None


async def test_pull_request_lookup_ignores_other_pull_requests() -> None:
    repo = InMemoryReviewJobRepository()
    other_ref = _REF.model_copy(update={"number": 999})
    await repo.create(await _completed_pr_job("k-1", other_ref))

    assert await repo.find_latest_completed_by_pull_request("acme/api", 431) is None


async def test_pull_request_lookup_ignores_jobs_with_no_pull_request() -> None:
    repo = InMemoryReviewJobRepository()
    await repo.create(await _completed_job("k-1", "issue-1"))  # no .pull_request set

    assert await repo.find_latest_completed_by_pull_request("acme/api", 431) is None


async def test_pull_request_lookup_picks_the_most_recent() -> None:
    repo = InMemoryReviewJobRepository()
    older = await repo.create(await _completed_pr_job("k-1", _REF))
    newer = await repo.create(await _completed_pr_job("k-2", _REF))
    newer.created_at = older.created_at.replace(year=older.created_at.year + 1)
    await repo.save(newer)

    found = await repo.find_latest_completed_by_pull_request("acme/api", 431)

    assert found.id == newer.id


# --- find_by_merge_confirmation_token (security fix, CLAUDE.md §1a) ---------


async def test_finds_the_job_for_a_merge_confirmation_token() -> None:
    repo = InMemoryReviewJobRepository()
    job = await repo.create(await _completed_job("k-1", "issue-1"))
    job.mark_merge_pending()
    await repo.save(job)

    found = await repo.find_by_merge_confirmation_token(job.merge_confirmation_token)

    assert found is not None
    assert found.id == job.id


async def test_merge_confirmation_token_lookup_does_not_match_by_id() -> None:
    """The whole point of the token: a job's own `id` must not work as its
    confirmation token."""
    repo = InMemoryReviewJobRepository()
    job = await repo.create(await _completed_job("k-1", "issue-1"))
    job.mark_merge_pending()
    await repo.save(job)

    assert await repo.find_by_merge_confirmation_token(job.id) is None


async def test_merge_confirmation_token_lookup_returns_none_for_unknown_token() -> None:
    assert await InMemoryReviewJobRepository().find_by_merge_confirmation_token("nope") is None


async def test_merge_confirmation_token_lookup_returns_none_before_pending() -> None:
    """A job that never entered PENDING_CONFIRMATION never minted a token."""
    repo = InMemoryReviewJobRepository()
    await repo.create(await _completed_job("k-1", "issue-1"))

    assert await repo.find_by_merge_confirmation_token(None) is None
