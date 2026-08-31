"""Protocol definitions — the dependency-inversion boundary.

Services and agents depend on these contracts, never on concrete clients. That
is what keeps three swaps cheap:

* GitHub REST client  ->  GitHub MCP server
* OpenAI agent runner ->  Anthropic agent runner
* Docker sandbox      ->  any other isolated executor

`typing.Protocol` (structural) rather than ABCs, so test fakes satisfy a
contract without inheriting from it.

All methods are async. The concrete clients wrap blocking work so a slow
sandbox or API call never stalls the event loop while other webhooks are in
flight.
"""

from typing import Protocol, runtime_checkable

from app.models.github import ChangedFile, Commit, PullRequest, PullRequestRef, RepositoryRef
from app.models.linear import LinearIssue
from app.models.posted_comment import PostedComment
from app.models.review import AgentRunOutcome, ReviewJob
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard
from app.models.sync_mapping import SyncMapping

# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


class SandboxExecResult(Protocol):
    """Result of one command executed inside a sandbox."""

    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class ISandbox(Protocol):
    """An isolated execution environment for untrusted repository content.

    Implementations must never be given a GitHub or Linear write credential.
    The host uploads inert PR bytes; the sandbox only reads and inspects them.
    """

    id: str

    async def upload_bytes(self, dest_path: str, content: bytes) -> None:
        """Write raw bytes to a path inside the sandbox."""
        ...

    async def exec(
        self, command: str, *, cwd: str | None = None, timeout_s: float | None = None
    ) -> SandboxExecResult:
        """Run a command inside the sandbox and capture its output."""
        ...

    async def extract_archive(self, archive_path: str, dest_dir: str) -> SandboxExecResult:
        """Extract a `.tar.gz` already uploaded at `archive_path` into `dest_dir`.

        Both paths are relative to the workspace root, same convention as
        `upload_bytes`/`read_file`. Implementations must apply a safe-extraction
        policy equivalent to `tarfile`'s `filter="data"` (PEP 706 — rejects
        `..`-escaping members, absolute paths, and symlinks/device files that
        would land outside `dest_dir`) rather than shelling out to a system
        `tar` with no such guarantee.
        """
        ...

    async def find_files(self, path: str, *, max_depth: int, limit: int) -> SandboxExecResult:
        """List files under `path` (relative to the workspace root), depth-bounded.

        `stdout` is at most `limit` newline-separated paths, each *relative to
        the workspace root* (e.g. `"repo/app/main.py"`, matching
        `ChangedFile.filename`'s own shape) — never an absolute host
        filesystem path, which would both fail to line up with anything the
        agent can compare it to and leak local machine layout into agent
        context and, ultimately, a published review comment.
        """
        ...

    async def grep_files(self, pattern: str, path: str, *, limit: int) -> SandboxExecResult:
        """Search for the literal string `pattern` (not a regex) under `path`.

        `stdout` is at most `limit` newline-separated `"path:lineno:line"`
        entries, `path` relative to the workspace root — same convention as
        `find_files`. `exit_code` follows `grep`'s own convention: `1` means
        "ran fine, no matches" (not a tool failure); `>= 2` means something
        actually broke.
        """
        ...

    async def read_file(self, path: str, *, max_bytes: int = 200_000) -> str:
        """Read a file from the sandbox, truncated to `max_bytes`."""
        ...

    async def destroy(self) -> None:
        """Tear down the sandbox. Idempotent; must not raise if already gone."""
        ...


@runtime_checkable
class ISandboxFactory(Protocol):
    """Creates sandboxes for review jobs."""

    async def create(self, *, labels: dict[str, str] | None = None) -> ISandbox:
        """Provision a fresh sandbox.

        Raises `SandboxUnavailableError` when no backend is configured — the
        review path fails closed rather than running untrusted code on the host.
        """
        ...


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@runtime_checkable
class IGitHubClient(Protocol):
    """Everything AIDA-MATE needs from GitHub.

    Read operations gather analysis input; write operations publish results and
    run only on the trusted host after the sandbox is destroyed.
    """

    # -- Read --
    async def get_pull_request(self, ref: PullRequestRef) -> PullRequest: ...

    async def get_pull_request_files(self, ref: PullRequestRef) -> list[ChangedFile]: ...

    async def get_pull_request_diff(self, ref: PullRequestRef) -> str: ...

    async def get_pull_request_commits(self, ref: PullRequestRef) -> list[Commit]: ...

    async def get_repository_file(self, repo: RepositoryRef, path: str, ref: str | None = None) -> str: ...

    async def download_archive(self, repo: RepositoryRef, sha: str) -> bytes:
        """Fetch repository content at `sha` as a tarball.

        Used to move PR content into the sandbox without giving the sandbox a
        credential of its own.
        """
        ...

    # -- Write --
    async def ensure_labels_exist(self, repo: RepositoryRef, labels: dict[str, str]) -> None:
        """Create any missing labels, mapping name -> hex colour. Idempotent."""
        ...

    async def apply_labels(
        self, ref: PullRequestRef, labels: set[str], *, exclusive_prefixes: tuple[str, ...] = ()
    ) -> None:
        """Apply labels, first removing stale ones under `exclusive_prefixes`.

        Exclusivity matters for `risk:` — a PR must never carry `risk:low` and
        `risk:high` simultaneously after a re-review.
        """
        ...

    async def add_comment(self, ref: PullRequestRef, body: str, *, marker: str | None = None) -> int:
        """Post or update a comment. With `marker`, updates in place rather than
        posting a duplicate on re-review. Returns the comment ID."""
        ...

    async def merge_pull_request(self, ref: PullRequestRef, *, merge_method: str = "merge") -> None:
        """Merge a pull request into its base branch.

        The only write action that changes a PR's mergedness rather than its
        labels/comments — see the gated auto-merge action, CLAUDE.md §1a.
        Raises `PullRequestNotMergeableError` (a `GitHubError`) when GitHub
        reports the PR cannot be merged right now (conflicts, failing
        required checks, branch protection) — an expected outcome, not a bug.
        """
        ...

    async def close_issue(self, repo: RepositoryRef, number: int) -> None:
        """Close a GitHub Issue. See CLAUDE.md §1c — the reverse direction of
        a Linear issue (synced from a GitHub Issue) landing on Done."""
        ...


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------


@runtime_checkable
class ILinearClient(Protocol):
    """Everything AIDA-MATE needs from Linear."""

    async def get_issue(self, issue_id: str) -> LinearIssue: ...

    async def add_comment(self, issue_id: str, body: str) -> str:
        """Post a comment on the issue. Returns the comment ID."""
        ...

    async def delete_comment(self, comment_id: str) -> None:
        """Delete a comment AIDA-MATE previously posted."""
        ...

    async def ensure_labels_exist(self, team_id: str, labels: set[str]) -> dict[str, str]:
        """Create any missing labels on the team. Returns name -> label ID."""
        ...

    async def apply_labels(self, issue_id: str, label_ids: set[str]) -> None: ...

    async def find_done_state_id(self, team_id: str) -> str | None:
        """Resolve a team's completed-type workflow state id, if one exists.

        Used by the GitHub-merge-syncs-Linear-to-Done action (CLAUDE.md §1b).
        """
        ...

    async def update_issue_state(self, issue_id: str, state_id: str) -> None:
        """Move an issue to `state_id`. The first write to an issue's status —
        every other Linear write in this codebase is a comment or a label."""
        ...

    async def find_team_id_by_key(self, team_key: str) -> str | None:
        """Resolve a team's id from its human-readable key (e.g. "GIT").

        Used by the GitHub-Issues/vulnerabilities-sync action (CLAUDE.md §1c).
        """
        ...

    async def ensure_label_id(self, team_id: str, name: str) -> str:
        """Resolve a team's label id by name, creating it if it doesn't exist yet."""
        ...

    async def create_issue(
        self, team_id: str, title: str, description: str, *, label_ids: list[str] | None = None
    ) -> tuple[str, str]:
        """Create a new issue on `team_id`. Returns (issue_id, identifier)."""
        ...

    async def update_issue_content(
        self, issue_id: str, *, title: str | None = None, description: str | None = None
    ) -> None:
        """Update an existing issue's title and/or description."""
        ...


# ---------------------------------------------------------------------------
# Agent / model provider
# ---------------------------------------------------------------------------


@runtime_checkable
class IReviewAgentRunner(Protocol):
    """Runs the LLM analysis over a pull request.

    The provider-swap seam. `OpenAIAgentRunner` implements this now; an
    Anthropic implementation can be added without touching GitHub, Linear,
    the sandbox, or risk logic.

    Implementations return judgment (`AgentRunOutcome.analysis`) plus audit
    metadata about the run itself — never a risk level, label set, or
    human-review decision, and never a self-reported tool-call count.
    """

    async def analyze(
        self, pull_request: PullRequest, sandbox: ISandbox, *, review_id: str
    ) -> AgentRunOutcome: ...


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@runtime_checkable
class IReviewJobRepository(Protocol):
    """Stores review jobs.

    Two implementations satisfy this: `InMemoryReviewJobRepository` (fast,
    dependency-free, loses everything on restart) and
    `SqliteReviewJobRepository` (durable, and the only one where
    `recover_interrupted` can find anything). Nothing above this interface
    knows which is active.
    """

    async def create(self, job: ReviewJob) -> ReviewJob: ...

    async def create_or_get(self, job: ReviewJob) -> tuple[ReviewJob, bool]:
        """Insert `job`, or return an in-flight one sharing its idempotency key.

        Returns `(job, created)`. Must be atomic: two concurrent webhook
        deliveries cannot both observe "absent" and both create a job.
        """
        ...

    async def get(self, job_id: str) -> ReviewJob | None: ...

    async def get_by_idempotency_key(self, key: str) -> ReviewJob | None:
        """Look up an existing job by its idempotency key, if any."""
        ...

    async def find_by_delivery_id(self, delivery_id: str) -> ReviewJob | None:
        """Look up the job created from a given webhook delivery, if any."""
        ...

    async def find_completed_by_content_key(
        self, content_key: str, *, exclude: str | None = None
    ) -> ReviewJob | None:
        """Find a COMPLETED review of the same PR revision, if one exists."""
        ...

    async def find_latest_completed_by_linear_issue_id(self, linear_issue_id: str) -> ReviewJob | None:
        """Find the most recent COMPLETED review for a Linear issue, if any.

        Used by the gated auto-merge action (CLAUDE.md §1a) to find what to
        merge when an issue moves to Done — distinct from
        `find_completed_by_content_key`, which is keyed by PR revision, not
        by issue.
        """
        ...

    async def find_latest_completed_by_pull_request(
        self, repo_full_name: str, number: int
    ) -> ReviewJob | None:
        """Find the most recent COMPLETED review for a GitHub PR, if any.

        The reverse lookup of `find_latest_completed_by_linear_issue_id`, used
        by the GitHub-merge-syncs-Linear-to-Done action (CLAUDE.md §1b).
        """
        ...

    async def find_by_merge_confirmation_token(self, token: str) -> ReviewJob | None:
        """Find the job a merge-confirmation token belongs to, if any.

        Security-audit finding, fixed here: the merge-confirmation URL used to
        be keyed by `job.id`, but `id` is also returned by the unauthenticated
        `GET /reviews` listing — not actually secret. `merge_confirmation_token`
        (see `ReviewJob.mark_merge_pending`) is a separate token minted only
        for this purpose and never listed anywhere.
        """
        ...

    async def recover_interrupted(self) -> list[ReviewJob]:
        """Mark jobs left non-terminal by a crash as INTERRUPTED, and return them."""
        ...

    async def save(self, job: ReviewJob) -> ReviewJob:
        """Persist mutations to an existing job."""
        ...

    async def list_recent(self, limit: int = 50) -> list[ReviewJob]: ...


# ---------------------------------------------------------------------------
# GitHub Issues / Security alerts -> Linear sync mapping
# ---------------------------------------------------------------------------


@runtime_checkable
class ISyncMappingRepository(Protocol):
    """Stores the GitHub-object <-> Linear-issue dedup mapping. See CLAUDE.md §1c.

    Two implementations satisfy this, mirroring `IReviewJobRepository`:
    `InMemorySyncMappingRepository` and `SqliteSyncMappingRepository`.
    """

    async def find_by_fingerprint(self, fingerprint: str) -> SyncMapping | None:
        """Look up an existing mapping by its dedup fingerprint, if any."""
        ...

    async def find_by_linear_issue_id(self, linear_issue_id: str) -> SyncMapping | None:
        """Look up the mapping that created/updates a given Linear issue, if any.

        The reverse lookup of `find_by_fingerprint` — used by the
        Linear-Done-closes-the-GitHub-issue direction of the sync (CLAUDE.md §1c).
        """
        ...

    async def create(self, mapping: SyncMapping) -> tuple[SyncMapping, bool]:
        """Insert `mapping`, or return the existing row sharing its fingerprint.

        Returns `(mapping, created)`, mirroring `IReviewJobRepository.create_or_get`.
        The caller uses `created` to detect the rare case where a concurrent
        delivery for the same GitHub object already created a Linear issue —
        see `GitHubIssueSyncService._upsert`'s docstring for how that's handled.
        """
        ...

    async def save(self, mapping: SyncMapping) -> SyncMapping:
        """Persist mutations to an existing mapping."""
        ...


# ---------------------------------------------------------------------------
# Posted-comment delete-link bookkeeping
# ---------------------------------------------------------------------------


@runtime_checkable
class IPostedCommentRepository(Protocol):
    """Stores the delete-link token <-> Linear-comment-id mapping for every
    comment `LinearService.add_comment` posts.

    Point lookups only — no dedup, no enumeration, unlike `ISyncMappingRepository`:
    each comment gets a fresh token, and nothing ever needs to list every
    posted comment. Two implementations satisfy this:
    `InMemoryPostedCommentRepository` and `SqlitePostedCommentRepository`.
    """

    async def get(self, token: str) -> PostedComment | None: ...

    async def save(self, record: PostedComment) -> PostedComment: ...

    async def delete(self, token: str) -> None: ...


# ---------------------------------------------------------------------------
# Scheduled prompts
# ---------------------------------------------------------------------------


@runtime_checkable
class IScheduledPromptRepository(Protocol):
    """Stores scheduled prompts. See CLAUDE.md §1d.

    Two implementations satisfy this, mirroring `ISyncMappingRepository`:
    `InMemoryScheduledPromptRepository` and `SqliteScheduledPromptRepository`.
    """

    async def create(self, scheduled: ScheduledPrompt) -> ScheduledPrompt: ...

    async def save(self, scheduled: ScheduledPrompt) -> ScheduledPrompt:
        """Persist mutations to an existing schedule."""
        ...

    async def get(self, scheduled_id: str) -> ScheduledPrompt | None: ...

    async def list_all(self) -> list[ScheduledPrompt]:
        """Enumerate every schedule. The scheduler worker's per-tick due-check."""
        ...

    async def delete(self, scheduled_id: str) -> None: ...


@runtime_checkable
class IScheduledPromptDashboardRepository(Protocol):
    """Stores which Linear issue is the live dashboard for one team's schedules.

    Keyed by `(organization_id, team_id)` — one dashboard issue per team,
    not per organization. Two implementations satisfy this:
    `InMemoryScheduledPromptDashboardRepository` and
    `SqliteScheduledPromptDashboardRepository`.
    """

    async def get(self, organization_id: str, team_id: str) -> ScheduledPromptDashboard | None: ...

    async def save(self, dashboard: ScheduledPromptDashboard) -> ScheduledPromptDashboard:
        """Insert or replace the dashboard for its (organization, team)."""
        ...
