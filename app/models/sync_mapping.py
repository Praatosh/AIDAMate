"""The GitHub-object <-> Linear-issue mapping record. See CLAUDE.md §1c.

Distinct from `ReviewJob`: a `ReviewJob` tracks AIDA-MATE's own PR review
lifecycle. A `SyncMapping` tracks something else entirely — that a GitHub
Issue or security alert was mirrored into a Linear issue AIDA-MATE created,
so a later delivery for the same GitHub object updates that issue instead of
creating a duplicate.
"""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Current UTC time. Indirected so tests can patch it in one place."""
    return datetime.now(UTC)


class SyncMapping(BaseModel):
    """One GitHub object synced into one Linear issue."""

    id: str = Field(default_factory=lambda: str(uuid4()))

    fingerprint: str = Field(
        description=(
            "'github:{owner/repo}:{source_type}:{source_id}' — the dedup key. Stable across "
            "redeliveries of the same GitHub object, so a repeat delivery updates the existing "
            "Linear issue instead of creating a second one."
        )
    )
    source_type: str = Field(description="'issue', 'code_scan', 'dependabot', or 'secret_scan'.")
    source_id: str = Field(description="The GitHub issue/alert number, as a string.")
    repository: str = Field(description="'owner/repo'.")

    linear_issue_id: str | None = None
    pr_number: int | None = Field(default=None, description="Related PR, when one was found.")
    github_issue_number: int | None = Field(
        default=None, description="Related GitHub Issue, when applicable (security alerts only)."
    )
    github_url: str
    state: str = Field(description="The GitHub object's own state string, mirrored for display.")

    created_at: datetime = Field(default_factory=_utcnow)
    last_synced_at: datetime = Field(default_factory=_utcnow)

    def touch(self) -> None:
        """Mark this mapping as freshly re-synced."""
        self.last_synced_at = _utcnow()
