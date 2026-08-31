"""GitHub domain models.

Narrow, typed projections of the GitHub API payloads AIDA-MATE actually uses —
not faithful mirrors of GitHub's full schemas. Keeping them narrow means an API
shape change touches only the service layer that builds them.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileChangeStatus(StrEnum):
    """How a file was changed in a pull request."""

    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class RepositoryRef(BaseModel):
    """Identifies a GitHub repository."""

    model_config = ConfigDict(frozen=True)

    owner: str
    name: str

    @property
    def full_name(self) -> str:
        """`owner/name`, as GitHub renders it."""
        return f"{self.owner}/{self.name}"


class PullRequestRef(BaseModel):
    """Minimal pointer to a pull request, resolved from a Linear issue.

    Deliberately separate from `PullRequest`: the resolver produces this before
    any GitHub call is made, and it is what gets persisted on the review job.
    """

    model_config = ConfigDict(frozen=True)

    repository: RepositoryRef
    number: int = Field(gt=0)
    url: str

    @property
    def slug(self) -> str:
        """`owner/name#number`, for logs and idempotency keys."""
        return f"{self.repository.full_name}#{self.number}"


class ChangedFile(BaseModel):
    """One file changed by a pull request."""

    filename: str
    status: FileChangeStatus
    additions: int = 0
    deletions: int = 0
    previous_filename: str | None = None
    patch: str | None = Field(
        default=None,
        description="Unified diff hunk for this file. Absent for binary or very large files.",
    )


class Commit(BaseModel):
    """One commit in a pull request."""

    sha: str
    message: str
    author: str | None = None


class PullRequest(BaseModel):
    """A pull request with everything needed for analysis."""

    ref: PullRequestRef
    title: str
    body: str | None = None
    author: str | None = None
    state: str = "open"
    draft: bool = False

    base_ref: str
    head_ref: str
    head_sha: str = Field(
        description="Exact head commit SHA. Preferred over the mutable branch ref for checkout."
    )

    is_fork: bool = Field(
        default=False,
        description="Whether the head is on a fork — relevant to how far the change is trusted.",
    )

    changed_files: list[ChangedFile] = Field(default_factory=list)
    commits: list[Commit] = Field(default_factory=list)

    @property
    def total_additions(self) -> int:
        """Lines added across all changed files."""
        return sum(f.additions for f in self.changed_files)

    @property
    def total_deletions(self) -> int:
        """Lines removed across all changed files."""
        return sum(f.deletions for f in self.changed_files)


# ---------------------------------------------------------------------------
# GitHub Issues / Security alerts -> Linear sync (CLAUDE.md §1c)
# ---------------------------------------------------------------------------


class GitHubIssueEvent(BaseModel):
    """A normalized GitHub Issue, from an `issues` webhook delivery.

    Never a pull request — GitHub's `issues` webhook event only fires for
    genuine Issues (unlike the REST `/issues` list endpoint, which mixes PRs
    in and must be filtered by a `pull_request` key on each item).
    """

    number: int
    title: str
    body: str | None = None
    state: str
    labels: list[str] = Field(default_factory=list)
    author_login: str | None = None
    repository: RepositoryRef
    url: str


class SecurityAlertEvent(BaseModel):
    """A normalized security alert, from a code_scanning_alert /
    dependabot_alert / secret_scanning_alert webhook delivery.

    One shape for all three sources: they overlap enough (id, state, url, an
    optional commit) that three near-duplicate classes would add nothing —
    `details` is only ever rendered into a Linear description, never queried
    structurally, so source-specific fields belong there rather than as their
    own typed attributes.
    """

    source_type: Literal["code_scan", "dependabot", "secret_scan"]
    alert_number: int
    state: str
    repository: RepositoryRef
    url: str
    commit_sha: str | None = None
    ref: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
