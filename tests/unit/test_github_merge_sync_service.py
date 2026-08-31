"""GitHub merge syncs the linked Linear issue to Done (CLAUDE.md §1b).

Uses the real `InMemoryReviewJobRepository` (simple enough not to need a
fake) plus a small recording fake for Linear, mirroring the style in
`tests/unit/test_auto_merge_service.py`.
"""

import pytest

from app.core.errors import LinearError
from app.models.common import ReviewJobStatus
from app.models.github import PullRequestRef, RepositoryRef
from app.models.linear import LinearIssue
from app.models.review import ReviewJob
from app.services.github_merge_sync_service import GitHubMergeSyncService
from app.services.job_repository import InMemoryReviewJobRepository

REF = PullRequestRef(
    repository=RepositoryRef(owner="acme", name="api"), number=431, url="https://github.com/acme/api/pull/431"
)


class FakeLinear:
    """Records issue-state reads/writes instead of calling Linear."""

    def __init__(self, *, team_id: str | None = "team-1", done_state_id: str | None = "state-done") -> None:
        self._team_id = team_id
        self._done_state_id = done_state_id
        self.updated: list[tuple[str, str]] = []

    async def get_issue(self, issue_id: str, *, organization_id=None) -> LinearIssue:
        return LinearIssue(id=issue_id, identifier="ENG-1", title="x", team_id=self._team_id)

    async def find_done_state_id(self, team_id: str, *, organization_id=None) -> str | None:
        return self._done_state_id

    async def update_issue_state(self, issue_id: str, state_id: str, *, organization_id=None) -> None:
        self.updated.append((issue_id, state_id))


@pytest.fixture
def repo() -> InMemoryReviewJobRepository:
    return InMemoryReviewJobRepository()


async def _completed_job(repo: InMemoryReviewJobRepository) -> ReviewJob:
    job = ReviewJob(idempotency_key="k-1", linear_issue_id="issue-1", status=ReviewJobStatus.COMPLETED)
    job.pull_request = REF
    return await repo.create(job)


async def test_syncs_to_done_when_a_completed_review_links_the_pr(repo: InMemoryReviewJobRepository) -> None:
    job = await _completed_job(repo)
    linear = FakeLinear()
    service = GitHubMergeSyncService(repo, linear)

    await service.handle_pull_request_merged(REF)

    assert linear.updated == [(job.linear_issue_id, "state-done")]


async def test_unknown_pr_is_a_no_op(repo: InMemoryReviewJobRepository) -> None:
    linear = FakeLinear()
    service = GitHubMergeSyncService(repo, linear)

    await service.handle_pull_request_merged(REF)

    assert linear.updated == []


async def test_issue_with_no_team_is_a_no_op(repo: InMemoryReviewJobRepository) -> None:
    await _completed_job(repo)
    linear = FakeLinear(team_id=None)
    service = GitHubMergeSyncService(repo, linear)

    await service.handle_pull_request_merged(REF)

    assert linear.updated == []


async def test_no_completed_state_for_the_team_is_a_no_op(repo: InMemoryReviewJobRepository) -> None:
    await _completed_job(repo)
    linear = FakeLinear(done_state_id=None)
    service = GitHubMergeSyncService(repo, linear)

    await service.handle_pull_request_merged(REF)

    assert linear.updated == []


async def test_different_pr_is_a_no_op(repo: InMemoryReviewJobRepository) -> None:
    await _completed_job(repo)
    linear = FakeLinear()
    service = GitHubMergeSyncService(repo, linear)
    other_ref = REF.model_copy(update={"number": 999})

    await service.handle_pull_request_merged(other_ref)

    assert linear.updated == []


# --- Regression: a LinearError from any step must never propagate ------------
#
# Live-testing caught exactly this: a malformed workflowStates query raised a
# LinearError that escaped uncaught, crashed the webhook handler, and
# returned a 400 to GitHub instead of the promised 2xx.


class RaisingLinear(FakeLinear):
    def __init__(self, *, fail_on: str) -> None:
        super().__init__()
        self._fail_on = fail_on

    async def get_issue(self, issue_id: str, *, organization_id=None) -> LinearIssue:
        if self._fail_on == "get_issue":
            raise LinearError("boom")
        return await super().get_issue(issue_id, organization_id=organization_id)

    async def find_done_state_id(self, team_id: str, *, organization_id=None) -> str | None:
        if self._fail_on == "find_done_state_id":
            raise LinearError("boom")
        return await super().find_done_state_id(team_id, organization_id=organization_id)

    async def update_issue_state(self, issue_id: str, state_id: str, *, organization_id=None) -> None:
        if self._fail_on == "update_issue_state":
            raise LinearError("boom")
        await super().update_issue_state(issue_id, state_id, organization_id=organization_id)


@pytest.mark.parametrize("fail_on", ["get_issue", "find_done_state_id", "update_issue_state"])
async def test_linear_error_never_propagates(repo: InMemoryReviewJobRepository, fail_on: str) -> None:
    await _completed_job(repo)
    service = GitHubMergeSyncService(repo, RaisingLinear(fail_on=fail_on))

    await service.handle_pull_request_merged(REF)  # must not raise
