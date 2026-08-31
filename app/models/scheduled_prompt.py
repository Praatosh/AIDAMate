"""A timer-triggered prompt run against a repo snapshot. See CLAUDE.md §1d.

Distinct from `ReviewJob`/`SyncMapping`: neither is timer-driven. A
`ScheduledPrompt` is the only record in this codebase whose next action is
decided by wall-clock time rather than an inbound webhook.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Current UTC time. Indirected so tests can patch it in one place."""
    return datetime.now(UTC)


class ScheduledPrompt(BaseModel):
    """One prompt: what to run, where, when, how often, and where to post the result."""

    id: str = Field(default_factory=lambda: str(uuid4()))

    title: str
    prompt: str = Field(description="The task text, e.g. 'run a general security audit of this repo'.")
    repository: str = Field(description="'owner/repo'.")
    branch: str | None = Field(
        default=None, description="None -> resolve the repository's default branch on every run."
    )
    pr_number: int | None = Field(
        default=None,
        description="If set, run against this pull request's head commit "
        "instead of `branch`/the repo's default branch.",
    )

    frequency: Literal["once", "hourly", "daily", "weekly", "monthly"] = "daily"
    run_on_date: str | None = Field(
        default=None, description="ISO 'YYYY-MM-DD', evaluated in `timezone`. Required for 'once'."
    )
    interval_hours: int | None = Field(
        default=None, description="Fire every N hours (1-23). Required for 'hourly'."
    )
    day_of_week: int | None = Field(
        default=None, description="0=Monday..6=Sunday. Required for 'weekly'."
    )
    day_of_month: int | None = Field(
        default=None,
        description="1-31, clamped to the month's actual last day when it's shorter. Required for 'monthly'.",
    )
    run_at_time: str | None = Field(
        default=None,
        description="24h 'HH:MM', evaluated in `timezone`. Required for once/daily/weekly/monthly; "
        "ignored for 'hourly'.",
    )
    timezone: str = Field(description="An IANA zone name, e.g. 'Asia/Kolkata'.")

    linear_issue_id: str = Field(description="Where the run's output is posted as a comment.")
    organization_id: str | None = None

    enabled: bool = True
    last_run_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the last completed run — the due-check and no-double-fire "
        "guard for every frequency.",
    )

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def mark_run(self, at: datetime) -> None:
        """Claim `at` as the run instant, before execution starts — see the scheduler worker's docstring."""
        self.last_run_at = at.astimezone(UTC)
        self.updated_at = _utcnow()

    def touch(self) -> None:
        """Mark this schedule as freshly updated."""
        self.updated_at = _utcnow()
