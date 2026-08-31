"""ScheduledPromptDashboardService: `sync()` fans out to every team,
`ensure()` stays pinned to one team, and the never-raise guarantee
(CLAUDE.md §1d).

Small recording fakes for Linear and both repositories, same style as
`test_github_merge_sync_service.py`.
"""

import pytest

from app.core.errors import LinearError
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard
from app.services.scheduled_prompt_dashboard_repository import InMemoryScheduledPromptDashboardRepository
from app.services.scheduled_prompt_dashboard_service import ScheduledPromptDashboardService
from app.services.scheduled_prompt_repository import InMemoryScheduledPromptRepository


def _scheduled(**overrides) -> ScheduledPrompt:
    values = {
        "title": "Security audit",
        "prompt": "Run a general security audit of this repository.",
        "repository": "acme/api",
        "run_at_time": "09:00",
        "timezone": "Asia/Kolkata",
        "linear_issue_id": "issue-1",
        "organization_id": "org-1",
    }
    values.update(overrides)
    return ScheduledPrompt(**values)


class FakeLinear:
    """Records team lookups / issue creates / issue-content updates."""

    def __init__(
        self,
        *,
        team_id: str | None = "team-1",
        teams: list[dict[str, str]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self._team_id = team_id
        # By default, list_teams() returns the single `team_id` team, so
        # sync()'s fan-out degenerates to the old single-team behavior
        # unless a test explicitly seeds multiple teams.
        self._teams = teams if teams is not None else ([{"id": team_id, "key": "GIT"}] if team_id else [])
        self._fail_on = fail_on
        self.created: list[tuple[str, str, str]] = []  # (team_id, title, description)
        self.updated: list[tuple[str, str]] = []  # (issue_id, description)
        self._next_issue_number = 1

    async def find_team_id_by_key(self, team_key: str, *, organization_id=None) -> str | None:
        if self._fail_on == "find_team_id_by_key":
            raise LinearError("boom")
        return self._team_id

    async def list_teams(self, *, organization_id=None) -> list[dict[str, str]]:
        if self._fail_on == "list_teams":
            raise LinearError("boom")
        return self._teams

    async def create_issue(self, team_id: str, title: str, description: str, *, organization_id=None):
        if self._fail_on == "create_issue":
            raise LinearError("boom")
        self.created.append((team_id, title, description))
        identifier = f"GIT-{self._next_issue_number}"
        issue_id = f"issue-dash-{self._next_issue_number}"
        self._next_issue_number += 1
        return issue_id, identifier

    async def update_issue_content(self, issue_id: str, *, description: str, organization_id=None) -> None:
        if self._fail_on == "update_issue_content":
            raise LinearError("boom")
        self.updated.append((issue_id, description))


@pytest.fixture
def scheduled_repo() -> InMemoryScheduledPromptRepository:
    return InMemoryScheduledPromptRepository()


@pytest.fixture
def dashboard_repo() -> InMemoryScheduledPromptDashboardRepository:
    return InMemoryScheduledPromptDashboardRepository()


# --- sync() ----------------------------------------------------------------


async def test_creates_the_dashboard_when_none_exists(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    await scheduled_repo.create(_scheduled())
    linear = FakeLinear()
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")

    assert len(linear.created) == 1
    team_id, title, description = linear.created[0]
    assert team_id == "team-1"
    assert "Security audit" in description
    stored = await dashboard_repo.get("org-1", "team-1")
    assert stored is not None
    assert stored.linear_issue_id == "issue-dash-1"


async def test_updates_an_existing_dashboard_in_place(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    await scheduled_repo.create(_scheduled())
    await dashboard_repo.save(
        ScheduledPromptDashboard(organization_id="org-1", team_id="team-1", linear_issue_id="existing-issue")
    )
    linear = FakeLinear()
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")

    assert linear.created == []
    assert len(linear.updated) == 1
    issue_id, description = linear.updated[0]
    assert issue_id == "existing-issue"
    assert "Security audit" in description


async def test_only_includes_schedules_for_the_given_organization(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    await scheduled_repo.create(_scheduled(title="Org 1 schedule", organization_id="org-1"))
    await scheduled_repo.create(_scheduled(title="Org 2 schedule", organization_id="org-2"))
    linear = FakeLinear()
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")

    description = linear.created[0][2]
    assert "Org 1 schedule" in description
    assert "Org 2 schedule" not in description


async def test_syncs_every_team_with_identical_content(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    await scheduled_repo.create(_scheduled())
    linear = FakeLinear(
        teams=[
            {"id": "team-1", "key": "GIT"},
            {"id": "team-2", "key": "ENG"},
            {"id": "team-3", "key": "OPS"},
        ]
    )
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")

    assert len(linear.created) == 3
    team_ids = {team_id for team_id, _, _ in linear.created}
    assert team_ids == {"team-1", "team-2", "team-3"}
    descriptions = {description for _, _, description in linear.created}
    assert len(descriptions) == 1  # identical content broadcast to every team

    for team_id in team_ids:
        stored = await dashboard_repo.get("org-1", team_id)
        assert stored is not None


async def test_one_teams_failure_does_not_stop_the_others(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    await scheduled_repo.create(_scheduled())
    # Only "existing-issue" (team-2) is pre-seeded, so its update_issue_content
    # call is the one that fails; team-1 and team-3 still create successfully.
    await dashboard_repo.save(
        ScheduledPromptDashboard(organization_id="org-1", team_id="team-2", linear_issue_id="existing-issue")
    )
    linear = FakeLinear(
        teams=[
            {"id": "team-1", "key": "GIT"},
            {"id": "team-2", "key": "ENG"},
            {"id": "team-3", "key": "OPS"},
        ],
        fail_on="update_issue_content",
    )
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")  # must not raise

    created_team_ids = {team_id for team_id, _, _ in linear.created}
    assert created_team_ids == {"team-1", "team-3"}
    assert linear.updated == []  # team-2's update failed


async def test_no_teams_found_logs_and_does_not_create(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    await scheduled_repo.create(_scheduled())
    linear = FakeLinear(teams=[])
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")  # must not raise

    assert linear.created == []


@pytest.mark.parametrize("fail_on", ["list_teams", "create_issue", "update_issue_content"])
async def test_linear_error_never_propagates(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
    fail_on: str,
) -> None:
    await scheduled_repo.create(_scheduled())
    if fail_on == "update_issue_content":
        await dashboard_repo.save(
            ScheduledPromptDashboard(
                organization_id="org-1", team_id="team-1", linear_issue_id="existing-issue"
            )
        )
    linear = FakeLinear(fail_on=fail_on)
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")  # must not raise


async def test_syncing_with_no_schedules_still_creates_an_empty_state_dashboard(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    linear = FakeLinear()
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    await service.sync("org-1")

    assert "No scheduled prompts configured" in linear.created[0][2]


# --- ensure() ------------------------------------------------------------------


async def test_ensure_creates_the_dashboard_when_none_exists(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    linear = FakeLinear()
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    dashboard = await service.ensure("org-1")

    assert dashboard is not None
    assert dashboard.linear_issue_id == "issue-dash-1"
    assert len(linear.created) == 1


async def test_ensure_returns_the_existing_dashboard_without_recreating(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    await dashboard_repo.save(
        ScheduledPromptDashboard(organization_id="org-1", team_id="team-1", linear_issue_id="existing")
    )
    linear = FakeLinear()
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    dashboard = await service.ensure("org-1")

    assert dashboard.linear_issue_id == "existing"
    assert linear.created == []


async def test_ensure_ignores_other_teams_and_only_touches_the_configured_one(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    """`ensure()` stays pinned to `team_key`, unlike `sync()`'s fan-out."""
    linear = FakeLinear(teams=[{"id": "team-1", "key": "GIT"}, {"id": "team-2", "key": "ENG"}])
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    dashboard = await service.ensure("org-1")

    assert dashboard is not None
    assert len(linear.created) == 1
    assert linear.created[0][0] == "team-1"


async def test_ensure_returns_none_when_the_team_cannot_be_resolved(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    linear = FakeLinear(team_id=None)
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    assert await service.ensure("org-1") is None


async def test_ensure_returns_none_when_find_team_id_by_key_raises(
    scheduled_repo: InMemoryScheduledPromptRepository,
    dashboard_repo: InMemoryScheduledPromptDashboardRepository,
) -> None:
    """Regression: unlike `sync()` (covered by `test_linear_error_never_
    propagates`'s `list_teams` case), `ensure()`'s own `find_team_id_by_key`
    call wasn't wrapped — a transient LinearError here (as opposed to
    team_key cleanly resolving to nothing) escaped as a raw exception
    instead of the None its callers (the web form, DefaultRepoScheduleService)
    already handle gracefully."""
    linear = FakeLinear(fail_on="find_team_id_by_key")
    service = ScheduledPromptDashboardService(
        dashboard_repo, scheduled_repo, linear, team_key="GIT", base_url="http://localhost:8000"
    )

    assert await service.ensure("org-1") is None  # must not raise
