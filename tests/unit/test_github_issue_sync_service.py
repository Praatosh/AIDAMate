"""GitHub Issues / Security alerts -> Linear (CLAUDE.md §1c).

Uses the real `InMemorySyncMappingRepository` (simple enough not to need a
fake) plus small recording fakes for GitHub and Linear, mirroring
`tests/unit/test_github_merge_sync_service.py`'s style.
"""

import pytest

from app.core.errors import GitHubError, LinearError
from app.models.github import GitHubIssueEvent, RepositoryRef, SecurityAlertEvent
from app.models.linear import ReviewTrigger
from app.models.sync_mapping import SyncMapping
from app.services.github_issue_sync_service import GitHubIssueSyncService
from app.services.sync_mapping_repository import InMemorySyncMappingRepository

REPO = RepositoryRef(owner="acme", name="api")
TEAM_KEY = "GIT"


class FakeGitHub:
    def __init__(
        self,
        *,
        referencing_pr: int | None = None,
        commit_pr: int | None = None,
        fail=False,
        fail_close=False,
    ):
        self._referencing_pr = referencing_pr
        self._commit_pr = commit_pr
        self._fail = fail
        self._fail_close = fail_close
        self.searched: list[str] = []
        self.commits_looked_up: list[str] = []
        self.closed: list[tuple[str, int]] = []

    async def search_pull_requests_referencing(self, repo, text: str):
        if self._fail:
            raise GitHubError("boom")
        self.searched.append(text)
        return [{"number": self._referencing_pr}] if self._referencing_pr else []

    async def list_pull_requests_for_commit(self, repo, sha: str):
        if self._fail:
            raise GitHubError("boom")
        self.commits_looked_up.append(sha)
        return [{"number": self._commit_pr}] if self._commit_pr else []

    async def close_issue(self, repo: RepositoryRef, number: int) -> None:
        if self._fail_close:
            raise GitHubError("boom")
        self.closed.append((repo.full_name, number))


class FakeLinear:
    def __init__(
        self,
        *,
        team_id: str | None = "team-git",
        done_state_id: str | None = "state-done",
        fail_create=False,
        fail_update=False,
        fail_label=False,
        fail_close=False,
    ):
        self._team_id = team_id
        self._done_state_id = done_state_id
        self._fail_create = fail_create
        self._fail_update = fail_update
        self._fail_label = fail_label
        self._fail_close = fail_close
        self.created: list[tuple[str, str, str, list[str] | None]] = []
        self.updated: list[tuple[str, str | None, str | None]] = []
        self.labels_ensured: list[tuple[str, str]] = []
        self.states_updated: list[tuple[str, str]] = []
        self._next_id = 0

    async def find_team_id_by_key(self, team_key: str):
        return self._team_id if team_key == TEAM_KEY else None

    async def ensure_label_id(self, team_id: str, name: str) -> str:
        if self._fail_label:
            raise LinearError("boom")
        self.labels_ensured.append((team_id, name))
        return f"label-{name.lower().replace(' ', '-')}"

    async def create_issue(self, team_id: str, title: str, description: str, *, label_ids=None):
        if self._fail_create:
            raise LinearError("boom")
        self._next_id += 1
        issue_id = f"issue-{self._next_id}"
        self.created.append((team_id, title, description, label_ids))
        return issue_id, f"GIT-{self._next_id}"

    async def update_issue_content(self, issue_id: str, *, title=None, description=None):
        if self._fail_update:
            raise LinearError("boom")
        self.updated.append((issue_id, title, description))

    async def find_done_state_id(self, team_id: str):
        return self._done_state_id

    async def update_issue_state(self, issue_id: str, state_id: str) -> None:
        if self._fail_close:
            raise LinearError("boom")
        self.states_updated.append((issue_id, state_id))


def _issue_event(**overrides) -> GitHubIssueEvent:
    values = dict(
        number=42,
        title="Authentication fails for new users",
        body="Steps to reproduce...",
        state="open",
        labels=["bug"],
        author_login="alice",
        repository=REPO,
        url="https://github.com/acme/api/issues/42",
    )
    values.update(overrides)
    return GitHubIssueEvent(**values)


def _alert_event(source_type: str = "code_scan", **overrides) -> SecurityAlertEvent:
    values = dict(
        source_type=source_type,
        alert_number=31,
        state="open",
        repository=REPO,
        url="https://github.com/acme/api/security/code-scanning/31",
        commit_sha="abc123",
        ref="refs/heads/main",
        details={"rule_description": "SQL Injection", "severity": "high"},
    )
    values.update(overrides)
    return SecurityAlertEvent(**values)


class FakeDefaultScheduleService:
    def __init__(self) -> None:
        self.ensured: list[str] = []

    async def ensure_for_repository(self, repository: str) -> None:
        self.ensured.append(repository)


@pytest.fixture
def mappings() -> InMemorySyncMappingRepository:
    return InMemorySyncMappingRepository()


# --- GitHub Issues ---------------------------------------------------------


async def test_creates_a_linear_issue_for_a_new_github_issue(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event())

    assert len(linear.created) == 1
    team_id, title, description, label_ids = linear.created[0]
    assert team_id == "team-git"
    assert title == "[GitHub Issue] Authentication fails for new users"
    assert "GitHub Issue: #42" in description
    assert "Labels: bug" in description
    assert "Author: alice" in description
    assert "Related PR" not in description
    assert linear.labels_ensured == [("team-git", "GitHub Issue")]
    assert label_ids == ["label-github-issue"]


async def test_includes_the_related_pr_when_found(mappings) -> None:
    github = FakeGitHub(referencing_pr=125)
    linear = FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event())

    assert github.searched == ["#42"]
    _, _, description, _ = linear.created[0]
    assert "Related PR: #125" in description


async def test_resyncing_the_same_issue_updates_instead_of_duplicating(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event())
    await service.handle_issue_event(_issue_event(title="Authentication fails - updated"))

    assert len(linear.created) == 1
    assert len(linear.updated) == 1
    issue_id, title, _ = linear.updated[0]
    assert issue_id == "issue-1"
    assert title == "[GitHub Issue] Authentication fails - updated"


async def test_pr_search_failure_does_not_block_the_sync(mappings) -> None:
    github, linear = FakeGitHub(fail=True), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event())

    assert len(linear.created) == 1
    _, _, description, _ = linear.created[0]
    assert "Related PR" not in description


# --- Default schedule hook (CLAUDE.md §1c/§1d bridge) ----------------------------


async def test_fresh_create_ensures_a_default_schedule_for_the_repository(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    default_schedules = FakeDefaultScheduleService()
    service = GitHubIssueSyncService(
        mappings, github, linear, team_key=TEAM_KEY, default_schedule_service=default_schedules
    )

    await service.handle_issue_event(_issue_event())

    assert default_schedules.ensured == ["acme/api"]


async def test_updating_an_existing_mapping_does_not_reensure_a_default_schedule(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    default_schedules = FakeDefaultScheduleService()
    service = GitHubIssueSyncService(
        mappings, github, linear, team_key=TEAM_KEY, default_schedule_service=default_schedules
    )

    await service.handle_issue_event(_issue_event())
    await service.handle_issue_event(_issue_event(title="Authentication fails - updated"))

    assert default_schedules.ensured == ["acme/api"]  # only once, on the fresh create


async def test_no_default_schedule_service_configured_does_not_break_the_sync(mappings) -> None:
    """`default_schedule_service` defaults to None — every other test in this
    file constructs the service without it, so this just makes the
    backward-compat case explicit."""
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event())  # must not raise

    assert len(linear.created) == 1


# --- Closing follows GitHub -----------------------------------------------------


async def test_creating_an_already_closed_issue_closes_it_in_linear_too(mappings) -> None:
    """A GitHub issue can already be closed by the time its first webhook
    delivery arrives (e.g. closed right after opening) — the newly-created
    Linear issue must land in Done immediately, not stay open."""
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event(state="closed"))

    assert len(linear.created) == 1
    assert linear.states_updated == [("issue-1", "state-done")]


async def test_closing_an_existing_synced_issue_closes_it_in_linear(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event(state="open"))
    assert linear.states_updated == []

    await service.handle_issue_event(_issue_event(state="closed"))

    assert len(linear.created) == 1
    assert linear.states_updated == [("issue-1", "state-done")]


async def test_open_issue_does_not_touch_state(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event(state="open"))

    assert linear.states_updated == []


async def test_security_alerts_never_close_the_linear_issue(mappings) -> None:
    """`close_when_closed` is opt-in per call site — security alerts don't
    set it, since their state vocabulary ("fixed"/"dismissed") doesn't map
    onto "closed" the way a GitHub Issue's does."""
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_security_alert(_alert_event(state="closed"))

    assert linear.states_updated == []


async def test_close_failure_does_not_raise(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear(fail_close=True)
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event(state="closed"))  # must not raise

    assert len(linear.created) == 1
    assert linear.states_updated == []


async def test_no_completed_state_found_does_not_raise(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear(done_state_id=None)
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event(state="closed"))  # must not raise

    assert linear.states_updated == []


# --- Security alerts ---------------------------------------------------------


@pytest.mark.parametrize("source_type", ["code_scan", "dependabot", "secret_scan"])
async def test_creates_a_linear_issue_for_each_security_source(mappings, source_type: str) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_security_alert(_alert_event(source_type))

    assert len(linear.created) == 1
    _, title, description, label_ids = linear.created[0]
    assert "[Security]" in title
    assert "GitHub:" in description
    assert linear.labels_ensured == [("team-git", "Security")]
    assert label_ids == ["label-security"]


async def test_non_string_severity_does_not_crash_title_rendering(mappings) -> None:
    """Regression: `severity.title()` assumed a string; GitHub's own field
    types for this aren't verified against live traffic (see the module
    docstring), so a numeric/other severity value must not turn this into an
    unhandled 500 instead of a synced issue."""
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_security_alert(_alert_event(details={"severity": 9}))  # must not raise

    assert len(linear.created) == 1
    _, title, _, _ = linear.created[0]
    assert "9 severity" in title


async def test_code_scanning_alert_finds_its_pr_via_commit(mappings) -> None:
    github = FakeGitHub(commit_pr=125)
    linear = FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_security_alert(_alert_event())

    assert github.commits_looked_up == ["abc123"]
    _, _, description, _ = linear.created[0]
    assert "Related PR: #125" in description


async def test_alert_without_a_commit_sha_never_looks_up_a_pr(mappings) -> None:
    """Dependabot alerts typically have no commit SHA — no PR lookup attempted,
    per the spec's explicit 'do not force every vulnerability to have a PR.'"""
    github = FakeGitHub(commit_pr=125)
    linear = FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_security_alert(_alert_event("dependabot", commit_sha=None))

    assert github.commits_looked_up == []
    _, _, description, _ = linear.created[0]
    assert "Related PR" not in description


async def test_resyncing_the_same_alert_updates_instead_of_duplicating(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear()
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_security_alert(_alert_event())
    await service.handle_security_alert(_alert_event(state="fixed"))

    assert len(linear.created) == 1
    assert len(linear.updated) == 1


# --- No-ops / error handling --------------------------------------------------


async def test_missing_team_is_a_no_op(mappings) -> None:
    github, linear = FakeGitHub(), FakeLinear(team_id=None)
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event())

    assert linear.created == []


@pytest.mark.parametrize("fail_kwarg", ["fail_create", "fail_update", "fail_label"])
async def test_linear_error_never_propagates(mappings, fail_kwarg: str) -> None:
    github = FakeGitHub()
    linear = FakeLinear(**{fail_kwarg: True})
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    if fail_kwarg == "fail_update":
        # Prime an existing mapping so the second call takes the update path.
        working_linear = FakeLinear()
        await GitHubIssueSyncService(mappings, github, working_linear, team_key=TEAM_KEY).handle_issue_event(
            _issue_event()
        )

    await service.handle_issue_event(_issue_event())  # must not raise


async def test_label_failure_still_creates_the_issue_untagged(mappings) -> None:
    """Linear can reject `issueLabelCreate` for the app actor (a real,
    live-tested permission restriction) even though `issueCreate` succeeds.
    The issue's content is worth syncing even when the tag can't be — this
    must not be all-or-nothing."""
    github, linear = FakeGitHub(), FakeLinear(fail_label=True)
    service = GitHubIssueSyncService(mappings, github, linear, team_key=TEAM_KEY)

    await service.handle_issue_event(_issue_event())

    assert len(linear.created) == 1
    _, title, _, label_ids = linear.created[0]
    assert title == "[GitHub Issue] Authentication fails for new users"
    assert label_ids is None


# --- Linear Done closes the linked GitHub issue (the reverse direction) --------


def _trigger(issue_id: str = "linear-issue-1") -> ReviewTrigger:
    return ReviewTrigger(source="issue_done", issue_id=issue_id)


async def _synced_issue_mapping(mappings, *, linear_issue_id: str = "linear-issue-1") -> SyncMapping:
    mapping = SyncMapping(
        fingerprint="github:acme/api:issue:42",
        source_type="issue",
        source_id="42",
        repository="acme/api",
        linear_issue_id=linear_issue_id,
        github_url="https://github.com/acme/api/issues/42",
        state="open",
    )
    stored, _ = await mappings.create(mapping)
    return stored


async def test_linear_done_closes_the_linked_github_issue(mappings) -> None:
    await _synced_issue_mapping(mappings)
    github = FakeGitHub()
    service = GitHubIssueSyncService(mappings, github, FakeLinear(), team_key=TEAM_KEY)

    await service.handle_linear_issue_done(_trigger())

    assert github.closed == [("acme/api", 42)]


async def test_unmapped_linear_issue_is_a_no_op(mappings) -> None:
    """Most Linear Done transitions have nothing to do with this sync at
    all — the common case, not an error."""
    github = FakeGitHub()
    service = GitHubIssueSyncService(mappings, github, FakeLinear(), team_key=TEAM_KEY)

    await service.handle_linear_issue_done(_trigger("some-other-issue"))

    assert github.closed == []


async def test_security_alert_mappings_are_not_closed(mappings) -> None:
    """Only plain GitHub Issues are closeable this way — a Linear issue
    synced from a security alert has no GitHub `state=closed` equivalent."""
    mapping = SyncMapping(
        fingerprint="github:acme/api:code_scan:7",
        source_type="code_scan",
        source_id="7",
        repository="acme/api",
        linear_issue_id="linear-issue-1",
        github_url="https://github.com/acme/api/security/code-scanning/7",
        state="open",
    )
    await mappings.create(mapping)
    github = FakeGitHub()
    service = GitHubIssueSyncService(mappings, github, FakeLinear(), team_key=TEAM_KEY)

    await service.handle_linear_issue_done(_trigger())

    assert github.closed == []


async def test_github_close_failure_does_not_raise(mappings) -> None:
    await _synced_issue_mapping(mappings)
    github = FakeGitHub(fail_close=True)
    service = GitHubIssueSyncService(mappings, github, FakeLinear(), team_key=TEAM_KEY)

    await service.handle_linear_issue_done(_trigger())  # must not raise

    assert github.closed == []
