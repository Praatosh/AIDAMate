"""Durable review job storage.

Runs against a real SQLite file in `tmp_path` — no mocking, because the two
properties worth proving here (surviving a restart, and the UNIQUE index
settling a race) only exist in the database itself.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.models.common import ReviewJobStatus
from app.models.github import PullRequestRef, RepositoryRef
from app.models.review import ReviewJob, build_content_key
from app.services.sqlite_job_repository import SqliteReviewJobRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "reviews.sqlite3"


@pytest.fixture
def repo(db_path: Path) -> SqliteReviewJobRepository:
    return SqliteReviewJobRepository(db_path)


def _job(key: str = "session:s-1", **overrides) -> ReviewJob:
    values = {"idempotency_key": key, "linear_issue_id": "issue-1"}
    values.update(overrides)
    return ReviewJob(**values)


# --- Round-trip --------------------------------------------------------------


async def test_create_then_get(repo: SqliteReviewJobRepository) -> None:
    job = await repo.create(_job())

    fetched = await repo.get(job.id)

    assert fetched is not None
    assert fetched.id == job.id
    assert fetched.linear_issue_id == "issue-1"


async def test_get_unknown_returns_none(repo: SqliteReviewJobRepository) -> None:
    assert await repo.get("nope") is None


async def test_save_persists_mutations(repo: SqliteReviewJobRepository) -> None:
    job = await repo.create(_job())
    job.mark_status(ReviewJobStatus.ANALYZING)
    job.head_sha = "abc123"

    await repo.save(job)

    fetched = await repo.get(job.id)
    assert fetched.status is ReviewJobStatus.ANALYZING
    assert fetched.head_sha == "abc123"


async def test_full_result_survives_the_round_trip(repo: SqliteReviewJobRepository) -> None:
    """The nested result is JSON in one column; it must come back intact."""
    from app.models.common import Area, RiskLevel, Severity
    from app.models.review import Finding, ReviewResult

    job = await repo.create(_job())
    job.result = ReviewResult(
        risk=RiskLevel.HIGH,
        risk_score=80,
        needs_human_review=True,
        findings=[
            Finding(
                category=Area.AUTHENTICATION,
                severity=Severity.HIGH,
                description="Token expiry not checked",
                file="auth.py",
                line=42,
            )
        ],
    )
    await repo.save(job)

    fetched = await repo.get(job.id)
    assert fetched.result.risk is RiskLevel.HIGH
    assert fetched.result.findings[0].line == 42


# --- Durability --------------------------------------------------------------


async def test_state_survives_a_new_repository_instance(db_path: Path) -> None:
    """The scenario this class exists for: a server restart."""
    job = await SqliteReviewJobRepository(db_path).create(_job())

    reopened = SqliteReviewJobRepository(db_path)

    assert (await reopened.get(job.id)) is not None


async def test_opening_a_fresh_database_is_empty(db_path: Path) -> None:
    assert await SqliteReviewJobRepository(db_path).list_recent() == []


# --- Idempotency -------------------------------------------------------------


async def test_create_or_get_collapses_an_in_flight_duplicate(
    repo: SqliteReviewJobRepository,
) -> None:
    first, created_first = await repo.create_or_get(_job())

    second, created_second = await repo.create_or_get(_job())

    assert created_first is True
    assert created_second is False
    assert second.id == first.id


async def test_a_terminal_job_releases_its_idempotency_key(
    repo: SqliteReviewJobRepository,
) -> None:
    """Matches the in-memory backend: a new trigger deserves a new job, and the
    content check downstream is what stops needless work."""
    first, _ = await repo.create_or_get(_job())
    first.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(first)

    second, created = await repo.create_or_get(_job())

    assert created is True
    assert second.id != first.id


async def test_a_completed_review_is_not_lost_when_the_key_is_reused(
    repo: SqliteReviewJobRepository,
) -> None:
    """Regression test: re-delegating an issue must not erase its prior
    review's history — only *actively racing* jobs are meant to collide.

    The bug this guards against: an earlier version of this store deleted the
    old row outright to free up the idempotency key, which silently destroyed
    the "GitHub/Linear always show the latest review, older ones stay
    historical" guarantee the very first time an already-reviewed issue was
    delegated again.
    """
    first, _ = await repo.create_or_get(_job())
    first.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(first)

    second, _ = await repo.create_or_get(_job())
    second.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(second)

    assert (await repo.get(first.id)) is not None
    assert (await repo.get(second.id)) is not None
    assert {job.id for job in await repo.list_recent()} == {first.id, second.id}


async def test_concurrent_creates_produce_exactly_one_job(
    repo: SqliteReviewJobRepository,
) -> None:
    """The race the UNIQUE index exists to settle."""
    results = await asyncio.gather(*(repo.create_or_get(_job()) for _ in range(8)))

    created = [job for job, was_created in results if was_created]
    assert len(created) == 1
    assert len({job.id for job, _ in results}) == 1


async def test_concurrent_creates_after_a_completed_job_all_see_it_as_blocking(
    repo: SqliteReviewJobRepository,
) -> None:
    """A subtler race: several new triggers arrive after the old job finished.
    Exactly one may create; the rest must collapse onto that one, not onto the
    old completed job (which the partial index no longer protects)."""
    first, _ = await repo.create_or_get(_job())
    first.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(first)

    results = await asyncio.gather(*(repo.create_or_get(_job()) for _ in range(8)))

    created = [job for job, was_created in results if was_created]
    assert len(created) == 1
    winner_id = created[0].id
    assert winner_id != first.id
    assert len({job.id for job, _ in results}) == 1
    assert {job.id for job, _ in results} == {winner_id}


# --- Lookups -----------------------------------------------------------------


async def test_find_by_delivery_id(repo: SqliteReviewJobRepository) -> None:
    job = await repo.create(_job(delivery_id="delivery-1"))

    assert (await repo.find_by_delivery_id("delivery-1")).id == job.id


async def test_find_by_unknown_delivery_id_returns_none(
    repo: SqliteReviewJobRepository,
) -> None:
    await repo.create(_job(delivery_id="delivery-1"))

    assert await repo.find_by_delivery_id("delivery-other") is None


async def test_find_completed_by_content_key(repo: SqliteReviewJobRepository) -> None:
    key = build_content_key("issue-1", 431, "sha-a")
    job = await repo.create(_job(content_key=key))
    job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(job)

    assert (await repo.find_completed_by_content_key(key)).id == job.id


async def test_find_completed_ignores_a_failed_review(repo: SqliteReviewJobRepository) -> None:
    key = build_content_key("issue-1", 431, "sha-a")
    job = await repo.create(_job(content_key=key))
    job.mark_failed("boom", "broke")
    await repo.save(job)

    assert await repo.find_completed_by_content_key(key) is None


async def test_find_completed_ignores_a_skipped_review(repo: SqliteReviewJobRepository) -> None:
    """Otherwise one skip would make every later trigger skip too."""
    key = build_content_key("issue-1", 431, "sha-a")
    job = await repo.create(_job(content_key=key))
    job.mark_skipped("already reviewed")
    await repo.save(job)

    assert await repo.find_completed_by_content_key(key) is None


async def test_find_completed_honours_exclude(repo: SqliteReviewJobRepository) -> None:
    """A job must never find *itself* and skip on that basis."""
    key = build_content_key("issue-1", 431, "sha-a")
    job = await repo.create(_job(content_key=key))
    job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(job)

    assert await repo.find_completed_by_content_key(key, exclude=job.id) is None


async def test_finds_the_completed_review_for_an_issue(repo: SqliteReviewJobRepository) -> None:
    job = await repo.create(_job(linear_issue_id="issue-1"))
    job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(job)

    found = await repo.find_latest_completed_by_linear_issue_id("issue-1")

    assert found is not None
    assert found.id == job.id


async def test_ignores_non_completed_jobs_for_the_issue(repo: SqliteReviewJobRepository) -> None:
    await repo.create(_job(linear_issue_id="issue-1"))  # stays QUEUED

    assert await repo.find_latest_completed_by_linear_issue_id("issue-1") is None


async def test_ignores_completed_jobs_for_other_issues(repo: SqliteReviewJobRepository) -> None:
    job = await repo.create(_job(linear_issue_id="issue-2"))
    job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(job)

    assert await repo.find_latest_completed_by_linear_issue_id("issue-1") is None


async def test_picks_the_most_recent_completed_job_for_the_issue(
    repo: SqliteReviewJobRepository,
) -> None:
    older = await repo.create(_job("session:a", linear_issue_id="issue-1"))
    older.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(older)

    newer = await repo.create(_job("session:b", linear_issue_id="issue-1"))
    newer.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(newer)

    found = await repo.find_latest_completed_by_linear_issue_id("issue-1")

    assert found.id == newer.id


# --- find_latest_completed_by_pull_request (CLAUDE.md §1b) -------------------

_REF = PullRequestRef(
    repository=RepositoryRef(owner="acme", name="api"), number=431, url="https://github.com/acme/api/pull/431"
)


async def test_finds_the_completed_review_for_a_pull_request(repo: SqliteReviewJobRepository) -> None:
    job = await repo.create(_job(pull_request=_REF))
    job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(job)

    found = await repo.find_latest_completed_by_pull_request("acme/api", 431)

    assert found is not None
    assert found.id == job.id


async def test_pull_request_lookup_ignores_non_completed_jobs(repo: SqliteReviewJobRepository) -> None:
    await repo.create(_job(pull_request=_REF))  # stays QUEUED

    assert await repo.find_latest_completed_by_pull_request("acme/api", 431) is None


async def test_pull_request_lookup_ignores_other_pull_requests(repo: SqliteReviewJobRepository) -> None:
    other_ref = _REF.model_copy(update={"number": 999})
    job = await repo.create(_job(pull_request=other_ref))
    job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(job)

    assert await repo.find_latest_completed_by_pull_request("acme/api", 431) is None


async def test_pull_request_lookup_ignores_jobs_with_no_pull_request(
    repo: SqliteReviewJobRepository,
) -> None:
    job = await repo.create(_job())  # no pull_request set
    job.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(job)

    assert await repo.find_latest_completed_by_pull_request("acme/api", 431) is None


async def test_pull_request_lookup_picks_the_most_recent(repo: SqliteReviewJobRepository) -> None:
    older = await repo.create(_job("session:a", pull_request=_REF))
    older.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(older)

    newer = await repo.create(_job("session:b", pull_request=_REF))
    newer.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(newer)

    found = await repo.find_latest_completed_by_pull_request("acme/api", 431)

    assert found.id == newer.id


async def test_list_recent_is_newest_first(repo: SqliteReviewJobRepository) -> None:
    older = await repo.create(_job("session:a"))
    newer = await repo.create(_job("session:b"))

    recent = await repo.list_recent()

    assert [job.id for job in recent] == [newer.id, older.id]


async def test_list_recent_respects_the_limit(repo: SqliteReviewJobRepository) -> None:
    for index in range(5):
        await repo.create(_job(f"session:{index}"))

    assert len(await repo.list_recent(limit=3)) == 3


# --- Restart recovery --------------------------------------------------------


async def test_recover_marks_a_mid_flight_job_interrupted(db_path: Path) -> None:
    repo = SqliteReviewJobRepository(db_path)
    job = await repo.create(_job())
    job.mark_status(ReviewJobStatus.ANALYZING)
    await repo.save(job)

    recovered = await SqliteReviewJobRepository(db_path).recover_interrupted()

    assert [item.id for item in recovered] == [job.id]
    assert (await repo.get(job.id)).status is ReviewJobStatus.INTERRUPTED


async def test_recovered_jobs_become_retryable(db_path: Path) -> None:
    """The point of recovery: an interrupted review must not be stuck forever."""
    repo = SqliteReviewJobRepository(db_path)
    job = await repo.create(_job())
    job.mark_status(ReviewJobStatus.PROVISIONING)
    await repo.save(job)

    await SqliteReviewJobRepository(db_path).recover_interrupted()

    assert (await repo.get(job.id)).status.is_retryable is True


async def test_recover_leaves_finished_jobs_alone(db_path: Path) -> None:
    repo = SqliteReviewJobRepository(db_path)
    done = await repo.create(_job("session:done"))
    done.mark_status(ReviewJobStatus.COMPLETED)
    await repo.save(done)

    recovered = await SqliteReviewJobRepository(db_path).recover_interrupted()

    assert recovered == []
    assert (await repo.get(done.id)).status is ReviewJobStatus.COMPLETED


async def test_recover_on_a_clean_database_is_a_no_op(repo: SqliteReviewJobRepository) -> None:
    assert await repo.recover_interrupted() == []


# --- find_by_merge_confirmation_token (security fix, CLAUDE.md §1a) ---------


async def test_finds_the_job_for_a_merge_confirmation_token(repo: SqliteReviewJobRepository) -> None:
    job = await repo.create(_job(linear_issue_id="issue-1"))
    job.mark_status(ReviewJobStatus.COMPLETED)
    job.mark_merge_pending()
    await repo.save(job)

    found = await repo.find_by_merge_confirmation_token(job.merge_confirmation_token)

    assert found is not None
    assert found.id == job.id


async def test_merge_confirmation_token_lookup_does_not_match_by_id(
    repo: SqliteReviewJobRepository,
) -> None:
    """The whole point of the token: a job's own `id` must not work as its
    confirmation token."""
    job = await repo.create(_job(linear_issue_id="issue-1"))
    job.mark_status(ReviewJobStatus.COMPLETED)
    job.mark_merge_pending()
    await repo.save(job)

    assert await repo.find_by_merge_confirmation_token(job.id) is None


async def test_merge_confirmation_token_lookup_returns_none_for_unknown_token(
    repo: SqliteReviewJobRepository,
) -> None:
    assert await repo.find_by_merge_confirmation_token("nope") is None


async def test_merge_confirmation_token_lookup_ignores_non_completed_jobs(
    repo: SqliteReviewJobRepository,
) -> None:
    """The pre-filter narrows the scan to COMPLETED jobs — a non-completed
    job (which could never have a token minted anyway) must not match."""
    await repo.create(_job(linear_issue_id="issue-1"))  # stays QUEUED

    assert await repo.find_by_merge_confirmation_token(None) is None


# --- Connection lifecycle (reliability-audit fix) -----------------------------


def test_connect_closes_the_connection_on_exit(repo: SqliteReviewJobRepository) -> None:
    """`_connect()` must actually close the connection, not just commit it.

    `sqlite3.Connection.__exit__` only handles the transaction (commit/
    rollback) — it never closes the connection on its own, which is
    documented sqlite3 behavior, not something `with conn:` alone fixes. A
    connection still open after the `with self._connect() as conn:` block
    exits is exactly the leak this test would have caught before the fix:
    using it afterward raises `ProgrammingError` only once it is genuinely
    closed.
    """
    with repo._connect() as conn:
        conn.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_closes_even_when_the_body_raises(repo: SqliteReviewJobRepository) -> None:
    """The `finally: conn.close()` must run on the error path too, not just
    the happy path — otherwise a failing operation would leak exactly the
    connections most likely to accumulate under real error conditions."""
    conn_holder: list[sqlite3.Connection] = []

    with pytest.raises(ValueError), repo._connect() as conn:
        conn_holder.append(conn)
        raise ValueError("boom")

    with pytest.raises(sqlite3.ProgrammingError):
        conn_holder[0].execute("SELECT 1")
