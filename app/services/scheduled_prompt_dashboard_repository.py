"""Scheduled-prompts-dashboard storage, keyed by (organization, team).

Simpler than `sync_mapping_repository.py`'s pair: there's no create/dedup
race to guard here — `ScheduledPromptDashboardService.sync` always runs one
team at a time, sequentially per call site (an API request or one worker
tick entry), so a plain get-then-save is enough.
"""

from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard


class InMemoryScheduledPromptDashboardRepository:
    """Process-local dashboard store."""

    def __init__(self) -> None:
        self._dashboards: dict[tuple[str, str], ScheduledPromptDashboard] = {}

    async def get(self, organization_id: str, team_id: str) -> ScheduledPromptDashboard | None:
        return self._dashboards.get((organization_id, team_id))

    async def save(self, dashboard: ScheduledPromptDashboard) -> ScheduledPromptDashboard:
        """Insert or replace the dashboard for its (organization, team)."""
        self._dashboards[(dashboard.organization_id, dashboard.team_id)] = dashboard
        return dashboard
