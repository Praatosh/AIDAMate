"""Linear domain models.

Covers three concerns: OAuth installations, inbound webhook events, and the
issue/attachment shapes AIDA-MATE reads.

On the agent trigger — Linear's first-class mechanism for agents is the
`AgentSessionEvent` webhook, fired when an app with the `app:assignable` scope
is delegated an issue or @-mentioned. A plain `Issue`/`update` event with a
changed `assigneeId` or `delegateId` is also supported as a secondary trigger,
because a workspace may assign an app user directly, or use Linear's in-app
"Delegate" action — the latter arrives as a plain `Issue` event in practice,
not an `AgentSessionEvent`. All three normalize to `ReviewTrigger` so nothing
downstream cares which arrived.

Reference: https://linear.app/developers/agent-interaction
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


class LinearInstallation(BaseModel):
    """An authorized Linear workspace.

    Tokens are `SecretStr`, so a stray `repr()`, log line, or error message
    renders `**********` instead of the credential. Call `.get_secret_value()`
    only at the point of use.
    """

    organization_id: str
    organization_name: str | None = None

    actor_id: str = Field(
        description=(
            "The app user's own Linear ID, discovered via `viewer { id }` at install time. "
            "Delegation and assignment events are matched against this."
        )
    )

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_expired(self, *, leeway: timedelta = timedelta(minutes=5)) -> bool:
        """Whether the access token is expired or close enough to warrant refreshing.

        The leeway avoids handing out a token that expires mid-request.
        """
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= (self.expires_at - leeway)


class AuthorizationRequest(BaseModel):
    """A prepared OAuth authorization redirect."""

    authorization_url: str
    state: str


class OAuthState(BaseModel):
    """Server-side record of a pending OAuth authorization.

    Exists to make `state` verifiable rather than decorative: the callback is
    only honoured if it presents a state this server issued, has not already
    consumed, and issued recently.
    """

    state: str
    code_verifier: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_expired(self, ttl: timedelta = timedelta(minutes=10)) -> bool:
        """Whether this authorization attempt has gone stale."""
        return datetime.now(UTC) >= (self.created_at + ttl)


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


class LinearAttachment(BaseModel):
    """An attachment on a Linear issue.

    Linear's GitHub integration surfaces linked pull requests here, which is how
    AIDA-MATE discovers the PR to review.
    """

    id: str | None = None
    url: str
    title: str | None = None
    source_type: str | None = Field(
        default=None,
        description="Origin, e.g. 'github'. Advisory only — URL shape is authoritative.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinearIssue(BaseModel):
    """A Linear issue, as much of it as AIDA-MATE needs."""

    id: str
    identifier: str = Field(description="Human-facing key such as 'ENG-123'.")
    title: str
    description: str | None = None
    url: str | None = None
    team_id: str | None = None
    assignee_id: str | None = None
    attachments: list[LinearAttachment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent activities
# ---------------------------------------------------------------------------


class AgentActivityType(StrEnum):
    """Activity kinds an agent can emit into a session.

    Linear derives the session's state from the most recent activity, so these
    are the agent's only way to report progress.
    """

    THOUGHT = "thought"
    ACTION = "action"
    RESPONSE = "response"
    ELICITATION = "elicitation"
    ERROR = "error"


class AgentSessionState(StrEnum):
    """States a Linear agent session can occupy."""

    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"
    AWAITING_INPUT = "awaitingInput"
    COMPLETE = "complete"
    STALE = "stale"


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

AGENT_SESSION_EVENT = "AgentSessionEvent"
ISSUE_EVENT = "Issue"


class LinearWebhookEvent(BaseModel):
    """A raw Linear webhook payload, parsed but not yet interpreted.

    Deliberately permissive: Linear sends many event shapes and AIDA-MATE ignores
    most of them. Rejecting unknown shapes at parse time would turn benign
    unrelated traffic into errors — and into webhook retries.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str | None = None
    action: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    updated_from: dict[str, Any] = Field(
        default_factory=dict,
        alias="updatedFrom",
        description=(
            "Previous values of fields this event changed. Essential for detecting that the "
            "assignee changed in *this* event rather than at some earlier point."
        ),
    )
    organization_id: str | None = Field(default=None, alias="organizationId")
    webhook_id: str | None = Field(default=None, alias="webhookId")
    webhook_timestamp: int | None = Field(
        default=None,
        alias="webhookTimestamp",
        description="UNIX milliseconds. Compared against local time to reject replays.",
    )

    @field_validator("data", "updated_from", mode="before")
    @classmethod
    def _coerce_to_dict(cls, value: object) -> dict[str, Any]:
        """Accept `null` or an unexpected type where an object was expected.

        A payload with `"data": null` should be *ignored* by the filter, not
        rejected as malformed — a 4xx would make Linear retry it repeatedly.
        Coercing to an empty dict lets the trigger extractor return None
        naturally.
        """
        return value if isinstance(value, dict) else {}


class ReviewTrigger(BaseModel):
    """A normalized 'something happened in Linear that AIDA-MATE should act on' signal.

    Produced by the webhook filter once an event has passed every check, so
    downstream code never re-validates. `agent_session_id`, when present,
    obliges AIDA-MATE to emit an acknowledging activity within ten seconds.

    `source="issue_done"` is not a review request — it never reaches
    `ReviewService.submit()` — it is the gated-auto-merge trigger (CLAUDE.md
    §1a), reusing this same normalized shape since it's built from the same
    webhook event.
    """

    model_config = ConfigDict(frozen=True)

    source: Literal["agent_session", "issue_assignment", "issue_auto", "issue_done"]
    issue_id: str
    issue_identifier: str | None = None
    agent_session_id: str | None = None
    prompt_context: str | None = None
    organization_id: str | None = None
    webhook_id: str | None = Field(
        default=None,
        description=(
            "Linear's webhook *subscription* id. Constant across every delivery from the same "
            "subscription, so it identifies the source, not the event — see `delivery_id`."
        ),
    )
    delivery_id: str | None = Field(
        default=None,
        description=(
            "Linear's per-delivery id, from the `Linear-Delivery` header. Unique per delivery "
            "attempt, so this — not `webhook_id` — is what makes redelivery detectable."
        ),
    )
