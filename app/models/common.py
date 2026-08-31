"""Shared taxonomy used across analysis, risk scoring, and labelling.

Deliberately a single vocabulary: a finding's *category* and a PR's *area* use
the same `Area` enum rather than two near-identical parallel taxonomies that
would inevitably drift apart.
"""

from enum import StrEnum


class RiskLevel(StrEnum):
    """Overall PR risk. Produced only by the deterministic risk engine."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Severity(StrEnum):
    """Severity of an individual finding.

    A distinct scale from `RiskLevel` on purpose: a PR can be MEDIUM risk
    overall while containing one CRITICAL finding. Collapsing the two would
    lose information. Severity is reported to humans but never multiplies the
    risk score — see `app/core/risk_engine.py`.
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MergeStatus(StrEnum):
    """Outcome of the gated auto-merge action for a completed review.

    Orthogonal to `ReviewJobStatus` — a job's review lifecycle (QUEUED -> ... ->
    COMPLETED) is a separate, already-settled fact from what later happens to
    its merge. `COMPLETED` keeps meaning exactly what it means today; this
    tracks a distinct decision made afterwards, by a human, in Linear.
    """

    PENDING_CONFIRMATION = "pending_confirmation"
    MERGED = "merged"
    DECLINED = "declined"
    FAILED = "failed"


class Area(StrEnum):
    """Areas of a codebase a change can touch.

    Doubles as the finding category vocabulary.

    Deliberately finer-grained than the `area: *` labels AIDA-MATE publishes.
    Risk scoring needs `payments` and `migrations` to be distinct from generic
    `backend` and `database` — they carry very different weights — but a
    reviewer glancing at a PR does not need nine near-synonymous labels. The
    internal taxonomy stays rich; `AREA_LABELS` narrows it for display.
    """

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    SECURITY = "security"
    BACKEND = "backend"
    FRONTEND = "frontend"
    API = "api"
    DATABASE = "database"
    MIGRATIONS = "migrations"
    INFRASTRUCTURE = "infrastructure"
    CI_CD = "ci-cd"
    PAYMENTS = "payments"
    BUSINESS_LOGIC = "business-logic"
    CONFIGURATION = "configuration"
    DEPENDENCIES = "dependencies"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    AI = "ai"


#: Internal area -> published label suffix.
#:
#: Several areas intentionally collapse onto one label: a migration is a
#: database change to a human reader, even though it scores far higher. Areas
#: absent from this map produce no `area: *` label — `configuration` and
#: `business-logic` are too vague to be useful on a PR.
AREA_LABELS: dict[Area, str] = {
    Area.AUTHENTICATION: "auth",
    Area.AUTHORIZATION: "auth",
    Area.SECURITY: "security",
    Area.BACKEND: "backend",
    Area.FRONTEND: "frontend",
    Area.API: "api",
    Area.DATABASE: "db",
    Area.MIGRATIONS: "db",
    Area.INFRASTRUCTURE: "infra",
    Area.CI_CD: "infra",
    Area.PAYMENTS: "payments",
    Area.DEPENDENCIES: "deps",
    Area.TESTING: "tests",
    Area.DOCUMENTATION: "docs",
    Area.AI: "ai",
}

#: Separator between a label's namespace and its value.
#:
#: GitHub labels render as `area: auth`. The space is part of the label name,
#: so it must match exactly — a mismatch silently creates a second, duplicate
#: label in every repository AIDA-MATE touches.
LABEL_SEPARATOR = ": "


def format_label(namespace: str, value: str) -> str:
    """Build a published label name, e.g. `("area", "auth") -> "area: auth"`."""
    return f"{namespace}{LABEL_SEPARATOR}{value}"


class ReviewJobStatus(StrEnum):
    """Lifecycle states of a review job.

    Progression is linear through the pipeline; FAILED is reachable from any
    non-terminal state.

    Three terminal states are distinct on purpose, because "nothing more will
    happen here" has three different meanings worth telling apart:

    * `COMPLETED` — reviewed, published.
    * `SKIPPED`  — this exact PR revision was already reviewed, so re-running
      would spend a sandbox and LLM tokens to reproduce an existing answer.
    * `FAILED` / `INTERRUPTED` — did not finish. `INTERRUPTED` specifically
      means the process died mid-review rather than the review itself going
      wrong, so it is a retry candidate rather than a reported defect.
    """

    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    FETCHING_PR = "FETCHING_PR"
    ANALYZING = "ANALYZING"
    CLASSIFYING = "CLASSIFYING"
    UPDATING_GITHUB = "UPDATING_GITHUB"
    UPDATING_LINEAR = "UPDATING_LINEAR"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"

    @property
    def is_terminal(self) -> bool:
        """Whether no further transition is expected from this state."""
        return self in (
            ReviewJobStatus.COMPLETED,
            ReviewJobStatus.SKIPPED,
            ReviewJobStatus.FAILED,
            ReviewJobStatus.INTERRUPTED,
        )

    @property
    def is_retryable(self) -> bool:
        """Whether an explicit retry of this job makes sense.

        A completed or deliberately skipped review has nothing to retry; a
        failed or interrupted one does.
        """
        return self in (ReviewJobStatus.FAILED, ReviewJobStatus.INTERRUPTED)
