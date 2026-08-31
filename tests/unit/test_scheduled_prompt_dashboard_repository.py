"""In-memory scheduled-prompts-dashboard repository (CLAUDE.md §1d)."""

from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard
from app.services.scheduled_prompt_dashboard_repository import InMemoryScheduledPromptDashboardRepository


def _dashboard(
    organization_id: str = "org-1", team_id: str = "team-1", linear_issue_id: str = "issue-dash-1"
) -> ScheduledPromptDashboard:
    return ScheduledPromptDashboard(
        organization_id=organization_id, team_id=team_id, linear_issue_id=linear_issue_id
    )


async def test_get_unknown_organization_returns_none() -> None:
    assert await InMemoryScheduledPromptDashboardRepository().get("nope", "team-1") is None


async def test_save_then_get() -> None:
    repo = InMemoryScheduledPromptDashboardRepository()
    saved = await repo.save(_dashboard())

    found = await repo.get("org-1", "team-1")
    assert found is saved
    assert found.linear_issue_id == "issue-dash-1"


async def test_save_upserts_by_organization_and_team_id() -> None:
    """A second save for the same (organization, team) replaces, not duplicates."""
    repo = InMemoryScheduledPromptDashboardRepository()
    await repo.save(_dashboard(linear_issue_id="issue-old"))
    await repo.save(_dashboard(linear_issue_id="issue-new"))

    found = await repo.get("org-1", "team-1")
    assert found.linear_issue_id == "issue-new"


async def test_distinct_organizations_get_distinct_dashboards() -> None:
    repo = InMemoryScheduledPromptDashboardRepository()
    await repo.save(_dashboard("org-1", "team-1", "issue-1"))
    await repo.save(_dashboard("org-2", "team-1", "issue-2"))

    assert (await repo.get("org-1", "team-1")).linear_issue_id == "issue-1"
    assert (await repo.get("org-2", "team-1")).linear_issue_id == "issue-2"


async def test_distinct_teams_in_the_same_organization_get_distinct_dashboards() -> None:
    repo = InMemoryScheduledPromptDashboardRepository()
    await repo.save(_dashboard("org-1", "team-1", "issue-1"))
    await repo.save(_dashboard("org-1", "team-2", "issue-2"))

    assert (await repo.get("org-1", "team-1")).linear_issue_id == "issue-1"
    assert (await repo.get("org-1", "team-2")).linear_issue_id == "issue-2"
