"""Review domain models: agent output, risk assessment, final result, and job.

The most important design point in this module is the boundary between
`ReviewAnalysis` and `ReviewResult`:

* `ReviewAnalysis` is what the **LLM** produces. It contains judgment —
  findings, affected areas, whether security is implicated.
* `ReviewResult` is what AIDA-MATE **publishes**. Its `risk`, `risk_score`,
  `needs_human_review`, and `labels` come from the deterministic engines.

`ReviewAnalysis` has no `risk` field at all. The model is not merely told not
to decide risk — its output schema makes doing so impossible.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.models.common import Area, MergeStatus, ReviewJobStatus, RiskLevel, Severity
from app.models.github import PullRequestRef


def _utcnow() -> datetime:
    """Current UTC time. Indirected so tests can patch it in one place."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Agent output — judgment only
# ---------------------------------------------------------------------------


class Finding(BaseModel):
    """A single observation about the pull request."""

    category: Area
    severity: Severity
    description: str = Field(description="What was observed.")
    reason: str = Field(default="", description="Why it matters.")
    file: str | None = None
    line: int | None = Field(default=None, gt=0)
    recommendation: str | None = None


class ReviewAnalysis(BaseModel):
    """Structured output of the review agent.

    Note the absence of `risk`, `risk_score`, `labels`, and
    `needs_human_review` — those are policy, not judgment, and are computed
    downstream by `core/risk_engine.py` and `core/label_engine.py`.
    """

    summary: str
    findings: list[Finding] = Field(default_factory=list)
    areas: list[Area] = Field(default_factory=list)
    security_impact: bool = False
    owasp_relevant: bool = False


class PRContextAnalysis(BaseModel):
    """Structured output of the Context Agent — orientation, not judgment.

    Purely a cost-control hint for the specialist agents that run after it
    (§39-40 of the multi-agent design: investigate the diff first, expand from
    there, rather than scanning everything regardless of relevance). No
    specialist's tool access is restricted by this — a specialist may still
    read any file — and a Context failure is non-fatal precisely because
    nothing downstream depends on it structurally, only helpfully.
    """

    summary: str
    intent: str = Field(description="What this PR is trying to accomplish, in plain language.")
    important_files: list[str] = Field(default_factory=list)
    affected_components: list[str] = Field(default_factory=list)


class AgentRunOutcome(BaseModel):
    """What actually happened during one agent run — audit metadata, not judgment.

    Kept separate from `ReviewAnalysis` for the same reason that model has no
    `risk` field: `tool_calls_count` is proof the agent investigated, and
    letting the model report it itself would make the proof worthless — a
    model could claim any number regardless of what it actually did. This is
    counted by `SandboxToolContext` as tool calls happen, not by asking.
    """

    analysis: ReviewAnalysis
    model: str
    tool_calls_count: int = Field(ge=0)
    failed_specialists: list[str] = Field(
        default_factory=list,
        description=(
            "Names of specialist agents (context/security/code/architecture/testing) that raised "
            "or timed out during this run. Empty means every stage that ran completed cleanly. "
            "Judge failure is never recorded here — it propagates and fails the whole review, "
            "since there is no reasonable partial result without a synthesis step."
        ),
    )


# ---------------------------------------------------------------------------
# Deterministic policy output
# ---------------------------------------------------------------------------


class ScoreContribution(BaseModel):
    """One rule firing during risk scoring, retained for auditability.

    Every point in the final score traces back to a named rule, so a HIGH
    classification can always be explained to the developer who receives it.
    """

    area: Area
    points: int
    rule: str = Field(description="Human-readable name of the rule that fired.")


class RiskAssessment(BaseModel):
    """Output of the deterministic risk engine."""

    score: int = Field(ge=0)
    level: RiskLevel
    needs_human_review: bool
    breakdown: list[ScoreContribution] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Published result
# ---------------------------------------------------------------------------


class ReviewResult(BaseModel):
    """The final, publishable review outcome.

    Assembled by the orchestrator from agent judgment plus engine policy; never
    produced wholesale by a model.
    """

    # From the risk / label engines
    risk: RiskLevel
    risk_score: int = Field(ge=0)
    needs_human_review: bool
    labels: list[str] = Field(default_factory=list)

    # From the agent
    areas: list[Area] = Field(default_factory=list)
    security_impact: bool = False
    owasp_relevant: bool = False
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    ai_analysis_ran: bool = Field(
        default=True,
        description=(
            "Whether the sandboxed AI agent actually executed for this review, as opposed to "
            "the deterministic path-based classification alone. False whenever the sandbox or "
            "model provider is unavailable; published text must not claim an AI reviewed the "
            "PR when this is False."
        ),
    )
    ai_model: str | None = Field(
        default=None,
        description="Model that produced the findings, when ai_analysis_ran is True. None otherwise.",
    )
    tool_calls_count: int = Field(
        default=0,
        ge=0,
        description=(
            "How many sandbox tool calls (list_files/read_file/search_code) the agent actually "
            "made, counted by our own code as calls happened — never self-reported by the model. "
            "Zero is a legitimate outcome (a very small PR may not need any), not evidence of "
            "a fake run; ai_analysis_ran is the field that distinguishes those two cases."
        ),
    )
    sandbox_mode: str | None = Field(
        default=None,
        description=(
            "'docker' or 'local', when ai_analysis_ran is True. 'local' is a host-filesystem "
            "stopgap, not container isolation — see LocalSandbox's own docstring. None when no "
            "agent ran."
        ),
    )
    failed_specialists: list[str] = Field(
        default_factory=list,
        description=(
            "Non-empty means the multi-agent pipeline ran but did not fully complete — some "
            "specialist(s) failed or timed out. Published text must say PARTIAL, not Completed, "
            "and needs_human_review is forced True regardless of the computed risk level: a gap "
            "in analysis coverage is exactly the situation a human should be told about."
        ),
    )
    analysis_reused: bool = Field(
        default=False,
        description=(
            "True when these findings were reused from a previous COMPLETED review of the exact "
            "same PR revision (same content_key) instead of re-running the AI stage. Explicit "
            "re-delegation always publishes a fresh comment, but the AI stage is not perfectly "
            "reproducible run-to-run — reusing the prior findings keeps the score stable across "
            "repeat re-delegations of an unchanged revision and avoids paying for a second "
            "sandbox + six agent calls when nothing about the revision changed."
        ),
    )

    # Provenance
    breakdown: list[ScoreContribution] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


def build_intake_key(issue_id: str, agent_session_id: str | None) -> str:
    """Build the dedup key available at webhook time.

    Deduplication happens in two stages, because the two things worth
    deduplicating are known at different moments:

    * **Intake** (this function) — stops a redelivered or duplicated webhook
      from starting a second concurrent run. All that is known here is the
      issue and, for agent triggers, the session.
    * **Content** (`build_content_key`) — stops re-reviewing a PR that has not
      changed. Requires the PR number and head SHA, which only exist after the
      resolver and GitHub fetch have run.

    A Linear agent session is unique per delegation, so it keys intake
    precisely. Plain assignments fall back to the issue.
    """
    if agent_session_id:
        return f"session:{agent_session_id}"
    return f"issue:{issue_id}"


def build_content_key(linear_issue_id: str, pr_number: int, head_sha: str | None) -> str:
    """Build the key identifying "this exact PR revision, for this issue".

    New commits produce a new key, which is correct: the PR genuinely changed
    and warrants a fresh review.
    """
    return f"{linear_issue_id}:{pr_number}:{head_sha or 'unknown'}"


class ReviewJob(BaseModel):
    """A unit of review work and its lifecycle state."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    idempotency_key: str

    # Trigger
    linear_issue_id: str
    linear_issue_identifier: str | None = None
    organization_id: str | None = None
    trigger_source: str | None = Field(
        default=None, description="'agent_session', 'issue_assignment', 'issue_auto', or 'retry'."
    )
    delivery_id: str | None = Field(
        default=None,
        description=(
            "Linear's per-delivery id (the `Linear-Delivery` header). Distinct from the payload's "
            "`webhookId`, which identifies the *subscription* and repeats across every delivery. "
            "Used to drop a redelivered event before it costs any GitHub API calls."
        ),
    )
    agent_session_id: str | None = Field(
        default=None,
        description=(
            "Linear agent session to report progress into. Present only for agent-delegated "
            "reviews; a plain assignment has no session to emit activities to."
        ),
    )

    # Target
    pull_request: PullRequestRef | None = None
    head_sha: str | None = None
    content_key: str | None = Field(
        default=None,
        description=(
            "`issue:pr:sha` — identifies this exact PR revision. Set once the PR is fetched; "
            "used to avoid re-reviewing an unchanged pull request."
        ),
    )

    # Lifecycle
    status: ReviewJobStatus = ReviewJobStatus.QUEUED
    sandbox_id: str | None = None
    attempt_number: int = Field(
        default=1,
        ge=1,
        description=(
            "Which attempt at this PR revision this job is. A retry keeps the same review "
            "identity (`content_key`) and increments this, so history is preserved rather "
            "than overwritten."
        ),
    )
    previous_review_id: str | None = Field(
        default=None,
        description="The job this one retries, if any. Chains attempts together for audit.",
    )
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Outcome
    risk_level: RiskLevel | None = None
    result: ReviewResult | None = None
    error_code: str | None = None
    error: str | None = Field(
        default=None,
        description="Developer-safe failure message. Must never contain secrets.",
    )

    # Gated auto-merge (see CLAUDE.md §1a) — orthogonal to `status` above; a
    # COMPLETED review's lifecycle stays a settled fact, this tracks a later,
    # separate human decision made in Linear.
    merge_status: MergeStatus | None = None
    merge_requested_at: datetime | None = None
    merge_decided_at: datetime | None = None
    merge_error: str | None = Field(
        default=None,
        description="Developer-safe message when a merge attempt failed (e.g. not mergeable).",
    )
    merge_confirmation_token: str | None = Field(
        default=None,
        description=(
            "A separate unguessable token for the merge-confirmation URL, distinct from `id`. "
            "`id` is returned by the unauthenticated GET /reviews listing, so using it as the "
            "confirmation page's own security wouldn't actually be secret — this token is never "
            "returned by any listing endpoint, only by the Linear comment the confirmation link "
            "is posted in. Security-audit finding, fixed here."
        ),
    )

    @property
    def duration_ms(self) -> int | None:
        """Wall-clock duration once the job has finished, else None."""
        if self.started_at is None or self.completed_at is None:
            return None
        return int((self.completed_at - self.started_at).total_seconds() * 1000)

    def touch(self) -> None:
        """Mark the job as modified now."""
        self.updated_at = _utcnow()

    def mark_status(self, status: ReviewJobStatus) -> None:
        """Advance the job to `status`, maintaining timing fields."""
        self.status = status
        if status is ReviewJobStatus.PROVISIONING and self.started_at is None:
            self.started_at = _utcnow()
        if status.is_terminal:
            self.completed_at = _utcnow()
        self.touch()

    def mark_failed(self, code: str, message: str) -> None:
        """Record a terminal failure with a developer-safe message."""
        self.error_code = code
        self.error = message
        self.mark_status(ReviewJobStatus.FAILED)

    def mark_skipped(self, reason: str) -> None:
        """Record that this revision needed no work, and why.

        Not a failure: `reason` explains a deliberate decision, so it is stored
        in `error` (the developer-safe message field) without an `error_code`.
        """
        self.error = reason
        self.mark_status(ReviewJobStatus.SKIPPED)

    def mark_interrupted(self) -> None:
        """Record that the process died before this job could finish.

        Distinct from `mark_failed`: nothing about the review went wrong, so it
        is a retry candidate rather than a defect to report to the requester.
        """
        self.error = "AIDA-MATE restarted while this review was in progress."
        self.mark_status(ReviewJobStatus.INTERRUPTED)

    def mark_merge_pending(self) -> None:
        """Record that a MEDIUM/HIGH-risk merge is waiting on human confirmation.

        Mints a fresh `merge_confirmation_token` — this is what the confirmation
        link's actual security rests on, not `id` (see that field's docstring).
        """
        self.merge_status = MergeStatus.PENDING_CONFIRMATION
        self.merge_requested_at = _utcnow()
        self.merge_confirmation_token = str(uuid4())
        self.touch()

    def mark_merged(self) -> None:
        """Record that the linked pull request was merged."""
        self.merge_status = MergeStatus.MERGED
        self.merge_decided_at = _utcnow()
        self.touch()

    def mark_merge_declined(self) -> None:
        """Record that a human declined the pending merge confirmation."""
        self.merge_status = MergeStatus.DECLINED
        self.merge_decided_at = _utcnow()
        self.touch()

    def mark_merge_failed(self, message: str) -> None:
        """Record that GitHub refused the merge (e.g. conflicts, failing checks)."""
        self.merge_status = MergeStatus.FAILED
        self.merge_error = message
        self.merge_decided_at = _utcnow()
        self.touch()

    def log_context(self) -> dict[str, Any]:
        """Fields attached to every log line for this job."""
        return {
            "review_id": self.id,
            "linear_issue_id": self.linear_issue_id,
            "agent_session_id": self.agent_session_id,
            "github_pr": self.pull_request.slug if self.pull_request else None,
            "head_sha": self.head_sha,
            "attempt": self.attempt_number,
            "sandbox_id": self.sandbox_id,
            "status": self.status.value,
            "risk": self.risk_level.value if self.risk_level else None,
            "duration_ms": self.duration_ms,
        }
