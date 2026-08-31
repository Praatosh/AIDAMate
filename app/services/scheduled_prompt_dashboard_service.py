"""Keeps one Linear issue per TEAM in sync as a live scheduled-prompts
dashboard. See CLAUDE.md §1d.

Find-or-create, then update-in-place — same shape as `GitHubIssueSyncService`
_upsert (§1c), reusing the same `LinearService` methods that already power
that sync (`find_team_id_by_key`/`list_teams`, `create_issue`,
`update_issue_content`).

`sync()` fans the SAME organization-wide schedule list out to every team in
the workspace (`list_teams`), so nobody has to know which one team happens
to host "the" dashboard. `ensure()` stays pinned to the single configured
`team_key` team — that's the one place a *new* schedule's results actually
post to; only the dashboard's read-only visibility fans out, not where
schedules are created.
"""

from app.core.errors import LinearError
from app.core.interfaces import IScheduledPromptDashboardRepository, IScheduledPromptRepository
from app.core.logging import get_logger
from app.core.scheduled_prompt_dashboard import DASHBOARD_ISSUE_TITLE, render_dashboard_description
from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard
from app.services.linear_service import LinearService

logger = get_logger(__name__)


class ScheduledPromptDashboardService:
    """Renders and pushes one organization's dashboard issue."""

    def __init__(
        self,
        dashboard_repository: IScheduledPromptDashboardRepository,
        scheduled_prompt_repository: IScheduledPromptRepository,
        linear: LinearService,
        *,
        team_key: str,
        base_url: str,
    ) -> None:
        self._dashboard_repository = dashboard_repository
        self._scheduled_prompt_repository = scheduled_prompt_repository
        self._linear = linear
        self._team_key = team_key
        # Passed straight through to `render_dashboard_description`, which
        # builds both the "create" link and each row's "Delete" link from it
        # fresh on every sync — see that function's docstring for why.
        self._base_url = base_url

    async def sync(self, organization_id: str) -> None:
        """Re-render and push every team's dashboard for `organization_id`.

        Never raises: called from both the CRUD API routes and the scheduler
        worker's tick loop, neither of which should fail or crash over a
        dashboard-sync hiccup. Listing teams and each team's own sync are
        both wrapped in their own `try/except LinearError` — the same lesson
        CLAUDE.md §8 already drew from `GitHubMergeSyncService`, and one
        team's failure (e.g. no create permission there) shouldn't stop the
        rest of the workspace from getting an up-to-date dashboard.
        """
        schedules = [
            scheduled
            for scheduled in await self._scheduled_prompt_repository.list_all()
            if scheduled.organization_id == organization_id
        ]
        description = render_dashboard_description(schedules, base_url=self._base_url)

        try:
            teams = await self._linear.list_teams(organization_id=organization_id)
        except LinearError as exc:
            logger.warning(
                "Could not list Linear teams; dashboard not synced",
                extra={"organization_id": organization_id, "error": str(exc)},
            )
            return

        for team in teams:
            team_id = team.get("id")
            if not team_id:
                continue
            await self._sync_one_team(organization_id, team_id, description)

    async def _sync_one_team(self, organization_id: str, team_id: str, description: str) -> None:
        """Get-or-create-then-update exactly one team's dashboard issue.

        Its own `try/except LinearError` so one team's failure doesn't stop
        `sync()`'s loop over the rest of the workspace's teams.
        """
        try:
            dashboard = await self._dashboard_repository.get(organization_id, team_id)
            if dashboard is None:
                issue_id, identifier = await self._linear.create_issue(
                    team_id, DASHBOARD_ISSUE_TITLE, description, organization_id=organization_id
                )
                await self._dashboard_repository.save(
                    ScheduledPromptDashboard(
                        organization_id=organization_id, team_id=team_id, linear_issue_id=issue_id
                    )
                )
                logger.info(
                    "Created the scheduled-prompts dashboard issue",
                    extra={
                        "organization_id": organization_id,
                        "team_id": team_id,
                        "linear_issue_identifier": identifier,
                    },
                )
            else:
                await self._linear.update_issue_content(
                    dashboard.linear_issue_id, description=description, organization_id=organization_id
                )
        except LinearError as exc:
            logger.warning(
                "Could not sync one team's scheduled-prompts dashboard",
                extra={"organization_id": organization_id, "team_id": team_id, "error": str(exc)},
            )

    async def ensure(self, organization_id: str) -> ScheduledPromptDashboard | None:
        """Return the configured team's dashboard for `organization_id`, creating
        it first if none exists yet.

        Used by the web form (`app/api/scheduled_prompt_form.py`) and
        `DefaultRepoScheduleService` to learn the dashboard's
        `linear_issue_id` a *new* schedule's results should post to.
        Deliberately pinned to the single configured `team_key` team, not
        `sync()`'s every-team fan-out — a schedule's results post to one
        place, even though the dashboard's read-only visibility fans out
        everywhere. Returns None only when resolving/creating that one
        team's dashboard failed (e.g. `team_key` doesn't resolve to a real
        team), mirroring `sync()`'s own no-op path for that case.
        """
        try:
            team_id = await self._linear.find_team_id_by_key(self._team_key, organization_id=organization_id)
        except LinearError as exc:
            # `sync()` catches this around `list_teams()`; this call site
            # was missing the equivalent, so a transient Linear failure here
            # (as opposed to team_key cleanly resolving to nothing) escaped
            # as a raw exception to ensure()'s callers (the web form,
            # DefaultRepoScheduleService) instead of the None they already
            # handle gracefully.
            logger.warning(
                "Could not resolve the dashboard's Linear team; dashboard not created",
                extra={"organization_id": organization_id, "team_key": self._team_key, "error": str(exc)},
            )
            return None
        if team_id is None:
            logger.warning(
                "Could not resolve the dashboard's Linear team; dashboard not created",
                extra={"organization_id": organization_id, "team_key": self._team_key},
            )
            return None

        dashboard = await self._dashboard_repository.get(organization_id, team_id)
        if dashboard is None:
            schedules = [
                scheduled
                for scheduled in await self._scheduled_prompt_repository.list_all()
                if scheduled.organization_id == organization_id
            ]
            description = render_dashboard_description(schedules, base_url=self._base_url)
            await self._sync_one_team(organization_id, team_id, description)
            dashboard = await self._dashboard_repository.get(organization_id, team_id)
        return dashboard
