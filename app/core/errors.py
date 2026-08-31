"""Domain exception hierarchy.

A single root (`AidaMateError`) lets the job runner distinguish expected,
explainable failures — which should be reported back to Linear in language a
developer can act on — from genuine bugs, which should surface as 500s and
alerts.
"""


class AidaMateError(Exception):
    """Base class for all AIDA-MATE domain errors."""

    #: Short, stable, machine-readable code recorded on the failed review job.
    code: str = "aida_mate_error"

    #: Message safe to show a developer in Linear/GitHub. Must never contain secrets.
    user_message: str = "AIDA-MATE encountered an unexpected error."


# --- Webhook / intake ------------------------------------------------------


class WebhookVerificationError(AidaMateError):
    """The webhook signature was missing, malformed, or did not match."""

    code = "webhook_verification_failed"
    user_message = "Webhook signature verification failed."


class WebhookPayloadError(AidaMateError):
    """The webhook body was not valid JSON or lacked required fields."""

    code = "webhook_payload_invalid"
    user_message = "Webhook payload was malformed."


# --- PR resolution ---------------------------------------------------------


class LinkedPullRequestNotFoundError(AidaMateError):
    """No GitHub pull request could be resolved from the Linear issue.

    Raised before any sandbox is provisioned, so a mis-linked issue never costs
    a sandbox.
    """

    code = "linked_pr_not_found"
    user_message = (
        "AIDA-MATE could not find a GitHub pull request linked to this issue. "
        "Link a PR to the issue and assign AIDA-MATE again."
    )


# --- External services -----------------------------------------------------


class ExternalServiceError(AidaMateError):
    """A third-party API call failed."""

    code = "external_service_error"
    user_message = "An external service call failed."


class GitHubError(ExternalServiceError):
    """GitHub API failure."""

    code = "github_error"
    user_message = "GitHub API request failed."


class GitHubRateLimitError(GitHubError):
    """GitHub rate limit exhausted; the operation is retryable later."""

    code = "github_rate_limited"
    user_message = "GitHub rate limit reached. AIDA-MATE will retry."


class GitHubUnavailableError(GitHubError):
    """GitHub could not be reached at all (connection refused, DNS, timeout).

    Distinct from `GitHubError`: this covers a request that never got a
    response, as opposed to a response GitHub actually sent back (403/404/5xx)
    — "unreachable" and "reachable but rejected" call for different handling,
    mirroring the rate-limit/forbidden distinction drawn above.
    """

    code = "github_unavailable"
    user_message = "GitHub could not be reached."


class GitHubServerError(GitHubError):
    """GitHub responded with a 5xx server error; likely transient, retryable."""

    code = "github_server_error"
    user_message = "GitHub returned a server error. AIDA-MATE will retry."


class InvalidPullRequestError(AidaMateError):
    """A resolved, fetched pull request turned out to be unreviewable.

    Covers: the PR is closed/merged, has zero changed files, or its head SHA
    no longer exists (a force-push race between resolving the PR and
    downloading its archive). Distinct from `LinkedPullRequestNotFoundError`,
    which fires when Linear points at nothing at all — this fires only after
    GitHub has already confirmed a PR exists but it can't be given a coherent
    snapshot to review.
    """

    code = "invalid_pr"
    user_message = "The pull request is not in a reviewable state."


class PullRequestNotMergeableError(GitHubError):
    """GitHub refused to merge a pull request — conflicts, failing required checks, or a
    branch-protection rule blocking it.

    A common, expected outcome of the gated auto-merge action (CLAUDE.md §1a), not a bug —
    kept distinct from a generic `GitHubError` so a caller can show a clear message instead
    of treating it as a failure.
    """

    code = "pr_not_mergeable"
    user_message = (
        "GitHub could not merge this pull request automatically — it may have conflicts, "
        "failing required checks, or a branch-protection rule blocking it."
    )


class LinearError(ExternalServiceError):
    """Linear API failure."""

    code = "linear_error"
    user_message = "Linear API request failed."


class LinearUnavailableError(LinearError):
    """Linear could not be reached at all; retryable."""

    code = "linear_unavailable"
    user_message = "Linear could not be reached."


class LinearServerError(LinearError):
    """Linear responded with a 5xx server error; likely transient, retryable."""

    code = "linear_server_error"
    user_message = "Linear returned a server error. AIDA-MATE will retry."


# --- Sandbox ---------------------------------------------------------------


class SandboxError(AidaMateError):
    """Sandbox lifecycle or execution failure."""

    code = "sandbox_error"
    user_message = "Sandboxed analysis failed."


class SandboxUnavailableError(SandboxError):
    """The `sbx` CLI is missing, or failed to create/run a sandbox.

    The agent-enrichment stage is optional (see `ReviewOrchestrator`) — this
    error fails only that stage's review, not AIDA-MATE's deterministic
    area/risk/label pipeline, which never needed a sandbox to begin with.
    """

    code = "sandbox_unavailable"
    user_message = (
        "AIDA-MATE could not create a sandbox for AI analysis. Install Docker Desktop, run "
        "'sbx login' once, and ensure Docker Desktop is running."
    )


class SandboxTimeoutError(SandboxError):
    """A sandbox command exceeded its time budget."""

    code = "sandbox_timeout"
    user_message = "Sandboxed analysis exceeded its time limit."


# --- Agent / model ---------------------------------------------------------


class AgentError(AidaMateError):
    """The analysis agent failed."""

    code = "agent_error"
    user_message = "AI analysis failed."


class AgentOutputError(AgentError):
    """The model returned output that did not satisfy the expected schema."""

    code = "agent_output_invalid"
    user_message = "AI analysis returned malformed output."


class AgentTimeoutError(AgentError):
    """The agent exceeded its time budget."""

    code = "agent_timeout"
    user_message = "AI analysis timed out."


# --- Build state -----------------------------------------------------------


class ReviewPipelineIncompleteError(AidaMateError):
    """The analysis pipeline is not fully implemented yet.

    A temporary, honest failure for phases where the job lifecycle exists but
    the stages it drives do not. Distinct from `SandboxUnavailableError` so a
    missing feature is never misreported as a missing credential.
    """

    code = "review_pipeline_incomplete"
    user_message = (
        "AIDA-MATE received this request, but its analysis pipeline is still being built and "
        "cannot review the pull request yet."
    )
