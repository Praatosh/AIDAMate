"""The per-team Scheduled Prompts dashboard record.

Distinct from `ScheduledPrompt`: this doesn't describe a schedule itself, only
which Linear issue is currently acting as the live dashboard for one
team's copy of the schedule list — the same "second dedup store, different
relationship" pattern `SyncMapping` established for §1c.

One row per `(organization_id, team_id)` pair, not per organization alone —
every team in the workspace gets its own dashboard issue, all showing the
same organization-wide schedule list, so nobody has to know which one team
happens to host "the" dashboard (CLAUDE.md §1d).
"""

from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Current UTC time. Indirected so tests can patch it in one place."""
    return datetime.now(UTC)


class ScheduledPromptDashboard(BaseModel):
    """One team's dashboard: which Linear issue it lives on."""

    organization_id: str = Field(description="Part of the natural key: (organization_id, team_id).")
    team_id: str = Field(description="Part of the natural key: (organization_id, team_id).")
    linear_issue_id: str

    updated_at: datetime = Field(default_factory=_utcnow)

    def touch(self) -> None:
        """Mark this dashboard as freshly re-synced."""
        self.updated_at = _utcnow()
