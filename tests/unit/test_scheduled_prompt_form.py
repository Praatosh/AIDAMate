"""The scheduled-prompt web form (CLAUDE.md §1d) — a second HTML page,
alongside the merge-confirmation dialog.

Uses the default `client` fixture (no GitHub/sandbox/Linear credentials).
Success-path tests inject a Linear installation into `linear_token_store`
(so organization resolution succeeds) and a fake dashboard service onto
`app.state` (so no real Linear call happens).
"""

import asyncio

import pytest

from app.api.scheduled_prompt_form import _parse_github_repository, _parse_pr_number
from app.models.linear import LinearInstallation
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard


class FakeDashboardService:
    """Records `ensure()`/`sync()` calls and returns a fixed dashboard, or None.

    `sync()` is needed too: a successful submission reuses
    `create_scheduled_prompt`, which calls it again after the schedule is saved.
    """

    def __init__(self, dashboard: ScheduledPromptDashboard | None) -> None:
        self._dashboard = dashboard
        self.ensured: list[str] = []
        self.synced: list[str] = []

    async def ensure(self, organization_id: str) -> ScheduledPromptDashboard | None:
        self.ensured.append(organization_id)
        return self._dashboard

    async def sync(self, organization_id: str) -> None:
        self.synced.append(organization_id)


def _install(client, organization_id: str = "org-1") -> None:
    installation = LinearInstallation(
        organization_id=organization_id, actor_id="actor-1", access_token="tok"
    )
    asyncio.run(client.app.state.linear_token_store.save(installation))


def test_get_form_renders_the_core_fields(client) -> None:
    response = client.get("/scheduled-prompts/new")

    assert response.status_code == 200
    assert 'name="prompt"' in response.text
    assert 'name="repository"' in response.text
    assert 'name="run_at_time"' in response.text
    assert 'type="time"' in response.text


def test_get_form_renders_the_frequency_fields(client) -> None:
    response = client.get("/scheduled-prompts/new")

    assert response.status_code == 200
    assert 'name="frequency"' in response.text
    assert 'name="run_on_date"' in response.text
    assert 'name="interval_hours"' in response.text
    assert 'name="day_of_week"' in response.text
    assert 'name="day_of_month"' in response.text
    for option in ("Once", "Daily", "Weekly", "Hourly", "Monthly"):
        assert option in response.text


def test_submit_without_a_resolvable_organization_shows_an_error(client) -> None:
    """Default test env has zero installed workspaces."""
    response = client.post(
        "/scheduled-prompts/new",
        data={"prompt": "Audit this repo", "repository": "acme/api", "run_at_time": "09:00"},
    )

    assert response.status_code == 200
    assert "Linear workspace" in response.text
    assert 'value="acme/api"' in response.text  # form re-populated, not lost


def test_submit_without_a_dashboard_service_configured_shows_an_error(client) -> None:
    _install(client)
    assert client.app.state.scheduled_prompt_dashboard_service is None

    response = client.post(
        "/scheduled-prompts/new",
        data={"prompt": "Audit this repo", "repository": "acme/api", "run_at_time": "09:00"},
    )

    assert response.status_code == 200
    assert "LINEAR_SYNC_TEAM_KEY" in response.text


def test_submit_when_the_dashboard_cannot_be_created_shows_an_error(client) -> None:
    _install(client)
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=None)

    response = client.post(
        "/scheduled-prompts/new",
        data={"prompt": "Audit this repo", "repository": "acme/api", "run_at_time": "09:00"},
    )

    assert response.status_code == 200
    assert "Could not create the dashboard issue" in response.text


def test_submit_rejects_a_non_github_url(client) -> None:
    _install(client)
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=dashboard)

    response = client.post(
        "/scheduled-prompts/new",
        data={"prompt": "Audit this repo", "repository": "not-a-github-url", "run_at_time": "09:00"},
    )

    assert response.status_code == 200
    assert "GitHub URL" in response.text
    # No schedule should have been created.
    assert asyncio.run(client.app.state.scheduled_prompt_repository.list_all()) == []


def test_submit_rejects_a_bare_owner_repo_slug(client) -> None:
    """The form takes a link now, not the `owner/repo` slug — typing the
    slug directly (no `github.com/`) must not silently work."""
    _install(client)
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=dashboard)

    response = client.post(
        "/scheduled-prompts/new",
        data={"prompt": "Audit this repo", "repository": "acme/api", "run_at_time": "09:00"},
    )

    assert response.status_code == 200
    assert "GitHub URL" in response.text
    assert asyncio.run(client.app.state.scheduled_prompt_repository.list_all()) == []


def test_successful_submission_creates_a_schedule_posting_to_the_dashboard(client) -> None:
    _install(client)
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    fake_dashboard_service = FakeDashboardService(dashboard=dashboard)
    client.app.state.scheduled_prompt_dashboard_service = fake_dashboard_service

    response = client.post(
        "/scheduled-prompts/new",
        data={
            "prompt": "Do a full security audit of this repository.",
            "repository": "https://github.com/acme/api",
            "run_at_time": "09:00",
        },
    )

    assert response.status_code == 200
    assert "Scheduled" in response.text
    assert fake_dashboard_service.ensured == ["org-1"]

    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert len(created) == 1
    schedule = created[0]
    assert schedule.prompt == "Do a full security audit of this repository."
    assert schedule.repository == "acme/api"  # parsed out of the URL
    assert schedule.run_at_time == "09:00"
    assert schedule.timezone == "Asia/Kolkata"
    assert schedule.linear_issue_id == "issue-dash-1"
    assert schedule.organization_id == "org-1"
    assert schedule.title  # derived, non-empty


def test_derived_title_is_the_full_prompt_untruncated(client) -> None:
    _install(client)
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=dashboard)
    long_prompt = "Investigate " + ("very " * 30) + "thoroughly for any security issues."

    client.post(
        "/scheduled-prompts/new",
        data={"prompt": long_prompt, "repository": "https://github.com/acme/api", "run_at_time": "09:00"},
    )

    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert created[0].title == long_prompt


def test_submitting_a_pr_link_targets_that_pr(client) -> None:
    """A PR URL in the repository field should be studied as that PR, not
    just the repo's default branch."""
    _install(client)
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=dashboard)

    response = client.post(
        "/scheduled-prompts/new",
        data={
            "prompt": "Review this pull request for security issues.",
            "repository": "https://github.com/acme/api/pull/123",
            "run_at_time": "09:00",
        },
    )

    assert response.status_code == 200
    assert "PR #123" in response.text

    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert len(created) == 1
    assert created[0].repository == "acme/api"
    assert created[0].pr_number == 123


def test_submitting_a_plain_repo_link_leaves_pr_number_unset(client) -> None:
    _install(client)
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=dashboard)

    client.post(
        "/scheduled-prompts/new",
        data={
            "prompt": "Audit this repo",
            "repository": "https://github.com/acme/api",
            "run_at_time": "09:00",
        },
    )

    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert created[0].pr_number is None


@pytest.mark.parametrize("field", ["prompt", "repository"])
def test_submit_missing_a_core_field_is_rejected(client, field: str) -> None:
    """`prompt`/`repository` stay required at the FastAPI parameter level
    regardless of frequency; a real 422, not a re-rendered form."""
    data = {"prompt": "Audit this repo", "repository": "https://github.com/acme/api", "run_at_time": "09:00"}
    del data[field]

    response = client.post("/scheduled-prompts/new", data=data)

    assert response.status_code == 422


def test_submit_missing_run_at_time_for_the_default_daily_frequency_shows_an_error(client) -> None:
    """Unlike `prompt`/`repository`, `run_at_time` is only required depending
    on `frequency` — checked by `ScheduledPromptCreate`'s cross-field
    validator, so a missing one here is a 200 error page, not a 422."""
    _install(client)
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=dashboard)

    response = client.post(
        "/scheduled-prompts/new",
        data={"prompt": "Audit this repo", "repository": "https://github.com/acme/api"},
    )

    assert response.status_code == 200
    assert "run_at_time" in response.text
    assert asyncio.run(client.app.state.scheduled_prompt_repository.list_all()) == []


# --- Frequency-specific submissions ---------------------------------------------


def _submit(client, **fields):
    dashboard = ScheduledPromptDashboard(
        organization_id="org-1", team_id="team-1", linear_issue_id="issue-dash-1"
    )
    client.app.state.scheduled_prompt_dashboard_service = FakeDashboardService(dashboard=dashboard)
    data = {"prompt": "Audit this repo", "repository": "https://github.com/acme/api", **fields}
    return client.post("/scheduled-prompts/new", data=data)


def test_submit_once_creates_a_schedule(client) -> None:
    _install(client)
    response = _submit(client, frequency="once", run_on_date="2026-08-25", run_at_time="09:00")

    assert response.status_code == 200
    assert "Scheduled" in response.text
    assert "once 2026-08-25 09:00 Asia/Kolkata" in response.text
    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert created[0].frequency == "once"
    assert created[0].run_on_date == "2026-08-25"


def test_submit_hourly_creates_a_schedule(client) -> None:
    _install(client)
    response = _submit(client, frequency="hourly", interval_hours="3")

    assert response.status_code == 200
    assert "every 3h" in response.text
    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert created[0].frequency == "hourly"
    assert created[0].interval_hours == 3
    assert created[0].run_at_time is None


def test_submit_weekly_creates_a_schedule(client) -> None:
    _install(client)
    response = _submit(client, frequency="weekly", day_of_week="2", run_at_time="09:00")

    assert response.status_code == 200
    assert "Wed 09:00 Asia/Kolkata (weekly)" in response.text
    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert created[0].frequency == "weekly"
    assert created[0].day_of_week == 2


def test_submit_monthly_creates_a_schedule(client) -> None:
    _install(client)
    response = _submit(client, frequency="monthly", day_of_month="1", run_at_time="09:00")

    assert response.status_code == 200
    assert "1st 09:00 Asia/Kolkata (monthly)" in response.text
    created = asyncio.run(client.app.state.scheduled_prompt_repository.list_all())
    assert created[0].frequency == "monthly"
    assert created[0].day_of_month == 1


def test_submit_hourly_without_interval_hours_shows_an_error(client) -> None:
    _install(client)
    response = _submit(client, frequency="hourly")

    assert response.status_code == 200
    assert "interval_hours" in response.text
    assert asyncio.run(client.app.state.scheduled_prompt_repository.list_all()) == []


def test_submit_with_a_non_numeric_interval_hours_shows_a_friendly_error(client) -> None:
    """A `display:none`-hidden field still submits its (blank) value in a real
    browser; a genuinely garbled one should read as a clear form error, not a
    raw 422 — see this module's docstring for why these fields are plain
    strings rather than typed `int | None = Form(...)` parameters."""
    _install(client)
    response = _submit(client, frequency="hourly", interval_hours="not-a-number")

    assert response.status_code == 200
    assert "whole numbers" in response.text
    assert asyncio.run(client.app.state.scheduled_prompt_repository.list_all()) == []


# --- _parse_github_repository ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/api",
        "http://github.com/acme/api",
        "github.com/acme/api",
        "https://www.github.com/acme/api",
        "https://github.com/acme/api/",
        "https://github.com/acme/api.git",
        "https://github.com/acme/api/tree/main",
        "https://github.com/acme/api?tab=readme",
        "  https://github.com/acme/api  ",
    ],
)
def test_parse_github_repository_accepts_common_url_shapes(url: str) -> None:
    assert _parse_github_repository(url) == "acme/api"


@pytest.mark.parametrize(
    "value",
    [
        "acme/api",  # bare slug, no link — no longer accepted
        "not a url at all",
        "https://gitlab.com/acme/api",  # wrong host
        "https://github.com/acme",  # no repo segment
        "https://github.com/",
    ],
)
def test_parse_github_repository_rejects_non_github_links(value: str) -> None:
    assert _parse_github_repository(value) is None


# --- _parse_pr_number -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/api/pull/123",
        "https://github.com/acme/api/pulls/123",
        "http://github.com/acme/api/pull/123",
        "github.com/acme/api/pull/123",
        "https://github.com/acme/api/pull/123/",
        "https://github.com/acme/api/pull/123/files",
        "https://github.com/acme/api/pull/123#discussion_r1",
        "  https://github.com/acme/api/pull/123  ",
    ],
)
def test_parse_pr_number_accepts_common_pr_url_shapes(url: str) -> None:
    assert _parse_pr_number(url) == 123


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/acme/api",  # plain repo link, no PR
        "https://github.com/acme/api/tree/main",
        "not a url at all",
        "https://github.com/acme/api/issues/123",  # an issue, not a PR
    ],
)
def test_parse_pr_number_returns_none_when_no_pr_is_targeted(value: str) -> None:
    assert _parse_pr_number(value) is None


# --- Delete confirmation ---------------------------------------------------------


def _seed_schedule(client, **overrides) -> ScheduledPrompt:
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
    scheduled = ScheduledPrompt(**values)
    return asyncio.run(client.app.state.scheduled_prompt_repository.create(scheduled))


def test_get_delete_confirmation_shows_the_schedules_details(client) -> None:
    scheduled = _seed_schedule(client)

    response = client.get(f"/scheduled-prompts/{scheduled.id}/delete")

    assert response.status_code == 200
    assert "Security audit" in response.text
    assert "acme/api" in response.text
    assert f'action="/scheduled-prompts/{scheduled.id}/delete"' in response.text


def test_delete_confirmation_page_sets_a_no_referrer_policy(client) -> None:
    """Security-audit fix: matches the same defense-in-depth `merge_confirmation.py`
    and `comment_deletion.py` already apply to their own id-in-URL pages."""
    scheduled = _seed_schedule(client)

    response = client.get(f"/scheduled-prompts/{scheduled.id}/delete")

    assert "name='referrer' content='no-referrer'" in response.text


def test_get_delete_confirmation_for_an_unknown_id_shows_not_found(client) -> None:
    response = client.get("/scheduled-prompts/does-not-exist/delete")

    assert response.status_code == 200
    assert "Not found" in response.text


def test_get_delete_confirmation_does_not_delete_anything(client) -> None:
    """The GET must be a pure read — a link preview or crawler fetching it
    must never trigger a real deletion."""
    scheduled = _seed_schedule(client)

    client.get(f"/scheduled-prompts/{scheduled.id}/delete")

    assert asyncio.run(client.app.state.scheduled_prompt_repository.get(scheduled.id)) is not None


def test_post_delete_removes_the_schedule(client) -> None:
    scheduled = _seed_schedule(client)

    response = client.post(f"/scheduled-prompts/{scheduled.id}/delete")

    assert response.status_code == 200
    assert "Deleted" in response.text
    assert "Security audit" in response.text
    assert asyncio.run(client.app.state.scheduled_prompt_repository.get(scheduled.id)) is None


def test_post_delete_for_an_unknown_id_shows_not_found(client) -> None:
    response = client.post("/scheduled-prompts/does-not-exist/delete")

    assert response.status_code == 200
    assert "Not found" in response.text


def test_post_delete_syncs_the_dashboard(client) -> None:
    scheduled = _seed_schedule(client)
    dashboard = FakeDashboardService(dashboard=None)
    client.app.state.scheduled_prompt_dashboard_service = dashboard

    client.post(f"/scheduled-prompts/{scheduled.id}/delete")

    assert dashboard.synced == ["org-1"]
