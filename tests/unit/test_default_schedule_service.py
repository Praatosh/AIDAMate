"""DefaultRepoScheduleService: ensure-at-least-one, idempotent, per repo
(CLAUDE.md §1c/§1d bridge).

Uses the real `InMemoryScheduledPromptRepository` (simple enough not to
need a fake) plus small recording fakes for the dashboard service and the
Linear token store, matching `test_scheduled_prompt_dashboard_service.py`'s
style.
"""

import asyncio

from app.models.linear import LinearInstallation
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard
from app.services.default_schedule_service import (
    DEFAULT_PROMPT,
    DEFAULT_RUN_AT_TIME,
    DEFAULT_TIMEZONE,
    DefaultRepoScheduleService,
)
from app.services.scheduled_prompt_repository import InMemoryScheduledPromptRepository


class FakeDashboardService:
    def __init__(self, dashboard: ScheduledPromptDashboard | None) -> None:
        self._dashboard = dashboard
        self.ensured: list[str] = []
        self.synced: list[str] = []

    async def ensure(self, organization_id: str) -> ScheduledPromptDashboard | None:
        self.ensured.append(organization_id)
        return self._dashboard

    async def sync(self, organization_id: str) -> None:
        self.synced.append(organization_id)


class FakeTokenStore:
    def __init__(self, installation: LinearInstallation | None) -> None:
        self._installation = installation

    async def get_default(self) -> LinearInstallation | None:
        return self._installation


def _installation(organization_id: str = "org-1") -> LinearInstallation:
    return LinearInstallation(organization_id=organization_id, actor_id="actor-1", access_token="tok")


async def test_creates_a_default_schedule_when_the_repo_has_none() -> None:
    repository = InMemoryScheduledPromptRepository()
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    dashboard_service = FakeDashboardService(dashboard)
    service = DefaultRepoScheduleService(repository, dashboard_service, FakeTokenStore(_installation()))

    await service.ensure_for_repository("acme/api")

    created = await repository.list_all()
    assert len(created) == 1
    scheduled = created[0]
    assert scheduled.repository == "acme/api"
    assert scheduled.prompt == DEFAULT_PROMPT
    assert scheduled.frequency == "daily"
    assert scheduled.run_at_time == DEFAULT_RUN_AT_TIME
    assert scheduled.timezone == DEFAULT_TIMEZONE
    assert scheduled.linear_issue_id == "issue-dash-1"
    assert scheduled.organization_id == "org-1"
    assert dashboard_service.ensured == ["org-1"]
    assert dashboard_service.synced == ["org-1"]


async def test_noop_when_the_repo_already_has_any_schedule() -> None:
    repository = InMemoryScheduledPromptRepository()
    await repository.create(
        ScheduledPrompt(
            title="Human-made",
            prompt="Something custom.",
            repository="acme/api",
            run_at_time="14:00",
            timezone="America/New_York",
            linear_issue_id="issue-1",
            organization_id="org-1",
        )
    )
    dashboard_service = FakeDashboardService(None)
    service = DefaultRepoScheduleService(repository, dashboard_service, FakeTokenStore(_installation()))

    await service.ensure_for_repository("acme/api")

    created = await repository.list_all()
    assert len(created) == 1
    assert created[0].title == "Human-made"  # untouched
    assert dashboard_service.ensured == []


async def test_noop_when_there_is_no_default_installation() -> None:
    repository = InMemoryScheduledPromptRepository()
    dashboard_service = FakeDashboardService(None)
    service = DefaultRepoScheduleService(repository, dashboard_service, FakeTokenStore(None))

    await service.ensure_for_repository("acme/api")

    assert await repository.list_all() == []
    assert dashboard_service.ensured == []


class SlowFakeDashboardService(FakeDashboardService):
    """Like `FakeDashboardService`, but yields control inside `ensure()` so a
    concurrent `ensure_for_repository` call gets a real chance to interleave
    — proving the lock actually serializes rather than merely happening not
    to overlap in this event loop's scheduling."""

    async def ensure(self, organization_id: str) -> ScheduledPromptDashboard | None:
        await asyncio.sleep(0)
        return await super().ensure(organization_id)


async def test_concurrent_calls_create_exactly_one_default_schedule() -> None:
    """Regression: `list_all()` + `create()` was not atomic — two GitHub
    objects in the same brand-new repo, each triggering their own concurrent
    `ensure_for_repository` call, could both observe no existing schedule
    and both create one."""
    repository = InMemoryScheduledPromptRepository()
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    dashboard_service = SlowFakeDashboardService(dashboard)
    service = DefaultRepoScheduleService(repository, dashboard_service, FakeTokenStore(_installation()))

    await asyncio.gather(
        service.ensure_for_repository("acme/api"),
        service.ensure_for_repository("acme/api"),
    )

    created = await repository.list_all()
    assert len(created) == 1


async def test_noop_when_the_dashboard_cannot_be_resolved() -> None:
    repository = InMemoryScheduledPromptRepository()
    dashboard_service = FakeDashboardService(None)
    service = DefaultRepoScheduleService(repository, dashboard_service, FakeTokenStore(_installation()))

    await service.ensure_for_repository("acme/api")

    assert await repository.list_all() == []
    assert dashboard_service.ensured == ["org-1"]
    assert dashboard_service.synced == []
