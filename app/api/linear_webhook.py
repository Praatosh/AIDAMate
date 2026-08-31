"""Linear webhook intake.

Handler sequence, in this order and no other:

    1. Verify the HMAC signature over the RAW body
    2. Reject stale deliveries (replay protection)
    3. Parse
    4. Filter to genuine review triggers
    5. Return 2xx fast

Verification precedes parsing because a signature computed over anything other
than the exact received bytes proves nothing — re-serializing parsed JSON can
change key order or whitespace and silently break the comparison.

The handler-sequence and timing-safe-comparison discipline follows the
`github-webhooks` skill; the provider specifics are Linear's and were verified
rather than assumed. Linear sends `Linear-Signature` carrying a bare
hex-encoded HMAC-SHA256 digest — not GitHub's `X-Hub-Signature-256` with its
`sha256=` prefix.

Two trigger shapes are recognized, both normalizing to `ReviewTrigger`:

* `AgentSessionEvent` / `created` — Linear's first-class agent mechanism, fired
  when AIDA-MATE is delegated an issue or @-mentioned. This is the primary path.
* `Issue` / `update` with `assigneeId` or `delegateId` in `updatedFrom` — a
  plain assignment or an in-app "Delegate to agent" action, supported as a
  secondary path. Confirmed against live traffic that Linear's "Delegate"
  button sends the latter (`delegateId` changing, `assigneeId` cleared) as a
  plain `Issue` event rather than an `AgentSessionEvent` — both fields are
  checked so either UI action reaches AIDA-MATE.

Reference: https://linear.app/developers/agent-interaction
"""

import hashlib
import hmac
import time
from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.linear import (
    AGENT_SESSION_EVENT,
    ISSUE_EVENT,
    LinearWebhookEvent,
    ReviewTrigger,
)

logger = get_logger(__name__)

router = APIRouter(tags=["webhooks"])

SIGNATURE_HEADER = "Linear-Signature"

#: Linear's per-delivery identifier. Note the payload's `webhookId` is *not*
#: this — that field names the subscription and repeats across every delivery,
#: so only this header can distinguish a redelivery from a new event.
DELIVERY_HEADER = "Linear-Delivery"

_AGENT_SESSION_TRIGGER_ACTIONS = frozenset({"created"})
_ISSUE_CREATE_ACTIONS = frozenset({"create"})
_ISSUE_UPDATE_ACTIONS = frozenset({"update"})
_ISSUE_AUTO_ACTIONS = frozenset({"create", "update"})
_ASSIGNEE_FIELD = "assigneeId"
_DELEGATE_FIELD = "delegateId"
_STATE_FIELD = "stateId"
_COMPLETED_STATE_TYPE = "completed"


class WebhookAck(BaseModel):
    """Response body for a webhook delivery."""

    accepted: bool
    queued: bool = False
    reason: str | None = None
    review_id: str | None = None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify Linear's HMAC-SHA256 signature over the raw request body.

    Compared with `hmac.compare_digest`, so the check takes the same time
    regardless of how many leading characters match. A naive `==` would leak,
    byte by byte, how close a guess was — enough to forge a signature given
    enough attempts.

    Args:
        raw_body: The exact bytes received, before any JSON parsing.
        signature_header: Value of the `Linear-Signature` header (bare hex).
        secret: The configured webhook signing secret.

    Returns:
        True only if the signature is present and matches.
    """
    if not signature_header or not secret:
        return False

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.strip(), expected)


def is_fresh(webhook_timestamp_ms: int | None, max_age_seconds: int, *, now_ms: int | None = None) -> bool:
    """Whether a delivery is recent enough to act on.

    A captured request with a valid signature stays valid forever unless its
    age is checked, so a replayed delivery could re-trigger a review at an
    attacker's chosen moment. Linear sends `webhookTimestamp` in UNIX
    milliseconds for exactly this purpose.

    A missing timestamp is treated as fresh: older Linear event types may omit
    it, and rejecting those would break legitimate traffic. The signature check
    remains the primary control.
    """
    if webhook_timestamp_ms is None:
        return True

    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    # Absolute difference, so a clock skewed into the future is caught too.
    return abs(current_ms - webhook_timestamp_ms) <= max_age_seconds * 1000


# ---------------------------------------------------------------------------
# Trigger extraction
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a payload fragment to a dict, tolerating nulls and wrong types."""
    return value if isinstance(value, dict) else {}


def _extract_agent_session_trigger(event: LinearWebhookEvent) -> ReviewTrigger | None:
    """Build a trigger from an `AgentSessionEvent`.

    Linear's public docs describe the payload's fields but not their exact
    nesting, so both plausible shapes are accepted: the session under
    `data.agentSession`, or `data` being the session itself. Fields such as
    `promptContext` are likewise looked for at both levels. Both shapes are
    covered by tests.
    """
    if event.action not in _AGENT_SESSION_TRIGGER_ACTIONS:
        return None

    data = _as_dict(event.data)
    session = _as_dict(data.get("agentSession")) or data
    issue = _as_dict(session.get("issue")) or _as_dict(data.get("issue"))

    issue_id = issue.get("id")
    session_id = session.get("id")
    if not issue_id:
        return None

    return ReviewTrigger(
        source="agent_session",
        issue_id=str(issue_id),
        issue_identifier=issue.get("identifier"),
        agent_session_id=str(session_id) if session_id else None,
        prompt_context=session.get("promptContext") or data.get("promptContext"),
        organization_id=event.organization_id,
        webhook_id=event.webhook_id,
    )


def _extract_issue_assignment_trigger(
    event: LinearWebhookEvent, aida_mate_actor_id: str | None
) -> ReviewTrigger | None:
    """Build a trigger from a plain assignee or delegate change to AIDA-MATE.

    Two shapes are recognized:

    * `update`: `assigneeId` or `delegateId` must appear in `updatedFrom` —
      meaning that field is what changed in *this* event. Without this,
      AIDA-MATE would re-review on every later unrelated edit to an
      already-assigned issue.
    * `create`: a brand-new issue can already have `assigneeId`/`delegateId`
      set to AIDA-MATE at creation time (e.g. Linear's issue-creation dialog
      lets you pick a delegate up front) — there is no `updatedFrom` to
      check for a new record, so the field's mere presence is the signal.

    Both fields are checked because Linear's UI offers two distinct actions
    that both mean "give this issue to AIDA-MATE" — classic assignment
    (`assigneeId`) and the app-actor "Delegate" action (`delegateId`, which
    Linear sends as a plain `Issue` event, not an `AgentSessionEvent`, and
    which clears `assigneeId` rather than setting it).
    """
    if not aida_mate_actor_id:
        return None

    data = _as_dict(event.data)

    if event.action in _ISSUE_CREATE_ACTIONS:
        matched = (
            data.get(_ASSIGNEE_FIELD) == aida_mate_actor_id or data.get(_DELEGATE_FIELD) == aida_mate_actor_id
        )
    elif event.action in _ISSUE_UPDATE_ACTIONS:
        matched = (
            _ASSIGNEE_FIELD in event.updated_from and data.get(_ASSIGNEE_FIELD) == aida_mate_actor_id
        ) or (_DELEGATE_FIELD in event.updated_from and data.get(_DELEGATE_FIELD) == aida_mate_actor_id)
    else:
        return None

    if not matched:
        return None

    issue_id = data.get("id")
    if not issue_id:
        return None

    return ReviewTrigger(
        source="issue_assignment",
        issue_id=str(issue_id),
        issue_identifier=data.get("identifier"),
        organization_id=event.organization_id,
        webhook_id=event.webhook_id,
    )


def _extract_issue_done_trigger(event: LinearWebhookEvent) -> ReviewTrigger | None:
    """Build a trigger when an issue's workflow state lands on a completed-type state.

    Not a review request — never reaches `review_service.submit()`. This is
    the gated-auto-merge trigger (CLAUDE.md §1a), dispatched separately by the
    webhook handler, only when `settings.auto_merge_on_done_enabled`.

    Unverified against live Linear traffic (unlike the assignee/delegate case
    above, which the file docstring notes was confirmed against real
    deliveries): assumes `updatedFrom.stateId` marks a state change the same
    way `assigneeId`/`delegateId` do, and that `data.state` is a nested
    `{id, name, type}` object. Matches on the NEW state's `type ==
    "completed"` rather than an exact state name, since workflow state names
    are workspace-configurable but `type` is a fixed Linear enum
    (backlog/unstarted/started/completed/canceled). Does not verify the OLD
    state was specifically "started" — that would need an extra Linear API
    call to resolve the old state id; "something changed AND landed on a
    completed-type state" is enough to satisfy "in progress converted to
    done" without that round trip.
    """
    if event.action not in _ISSUE_UPDATE_ACTIONS:
        return None
    if _STATE_FIELD not in event.updated_from:
        return None

    data = _as_dict(event.data)
    state = _as_dict(data.get("state"))
    if state.get("type") != _COMPLETED_STATE_TYPE:
        return None

    issue_id = data.get("id")
    if not issue_id:
        return None

    return ReviewTrigger(
        source="issue_done",
        issue_id=str(issue_id),
        issue_identifier=data.get("identifier"),
        organization_id=event.organization_id,
        webhook_id=event.webhook_id,
    )


def _extract_auto_trigger(event: LinearWebhookEvent) -> ReviewTrigger | None:
    """Build a trigger from any issue create/update, for automatic mode.

    The broadest path: every ticket touch becomes a candidate review. What
    keeps it affordable is downstream, not here — an issue with no linked PR
    stops at resolution, and a PR already reviewed at this head SHA stops at
    the content-key check. Neither reaches a sandbox.
    """
    if event.action not in _ISSUE_AUTO_ACTIONS:
        return None

    data = _as_dict(event.data)
    issue_id = data.get("id")
    if not issue_id:
        return None

    return ReviewTrigger(
        source="issue_auto",
        issue_id=str(issue_id),
        issue_identifier=data.get("identifier"),
        organization_id=event.organization_id,
        webhook_id=event.webhook_id,
    )


def extract_review_trigger(
    event: LinearWebhookEvent,
    aida_mate_actor_id: str | None,
    *,
    auto_review_enabled: bool = False,
) -> ReviewTrigger | None:
    """Return a `ReviewTrigger` if this event should start a review, else None.

    Returning None is the common case and is not an error: Linear sends far more
    traffic than AIDA-MATE cares about.

    Order matters. Delegation and explicit assignment are checked first so that
    a deliberate request is always attributed to the path the requester chose —
    an agent session, when there is one, gets progress activities that a
    background auto-review does not.
    """
    if event.type == AGENT_SESSION_EVENT:
        return _extract_agent_session_trigger(event)

    if event.type == ISSUE_EVENT:
        assignment = _extract_issue_assignment_trigger(event, aida_mate_actor_id)
        if assignment is not None:
            return assignment
        if auto_review_enabled:
            return _extract_auto_trigger(event)

    return None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/webhooks/linear", response_model=WebhookAck)
async def handle_linear_webhook(request: Request, response: Response) -> WebhookAck:
    """Receive a Linear webhook.

    Returns 2xx quickly in every non-auth case. Unrelated events are accepted
    and ignored rather than rejected — a 4xx would make Linear retry traffic
    AIDA-MATE simply does not want.
    """
    settings: Settings = request.app.state.settings

    raw_body = await request.body()

    if not verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER), settings.linear_webhook_secret):
        logger.warning("Rejected Linear webhook with invalid signature")
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return WebhookAck(accepted=False, reason="invalid_signature")

    try:
        event = LinearWebhookEvent.model_validate_json(raw_body)
    except ValidationError:
        logger.warning("Rejected Linear webhook with unparseable payload")
        response.status_code = status.HTTP_400_BAD_REQUEST
        return WebhookAck(accepted=False, reason="invalid_payload")

    if not is_fresh(event.webhook_timestamp, settings.linear_webhook_max_age_seconds):
        logger.warning(
            "Rejected stale Linear webhook",
            extra={"webhook_id": event.webhook_id, "webhook_timestamp": event.webhook_timestamp},
        )
        response.status_code = status.HTTP_400_BAD_REQUEST
        return WebhookAck(accepted=False, reason="stale_delivery")

    # The two separate, opt-in "issue landed on Done" paths — gated auto-merge
    # (CLAUDE.md §1a, keyed off a ReviewJob) and closing a linked GitHub Issue
    # back (§1c's reverse direction, keyed off a SyncMapping) — are checked
    # unconditionally, before (and independent of) `extract_review_trigger`
    # below. They used to run only when no review trigger was also found for
    # this delivery, which silently broke both whenever LINEAR_AUTO_REVIEW_
    # ENABLED was on: a Done-type state change is itself an "update" action,
    # so `_extract_auto_trigger` would produce a review trigger for that same
    # event and short-circuit past the done-trigger check entirely. Both may
    # fire for the same delivery if somehow both apply, though in practice a
    # given Linear issue is normally one or the other, never both. Awaited
    # inline rather than queued: at most one Linear/GitHub call each, well
    # within the 10s ack budget, and neither runs a sandboxed review.
    done_trigger = _extract_issue_done_trigger(event)
    if done_trigger is not None:
        done_trigger = done_trigger.model_copy(update={"delivery_id": request.headers.get(DELIVERY_HEADER)})
        if settings.auto_merge_on_done_enabled and request.app.state.auto_merge_service is not None:
            await request.app.state.auto_merge_service.handle_issue_done(done_trigger)
        if settings.github_issue_sync_enabled and request.app.state.github_issue_sync_service is not None:
            await request.app.state.github_issue_sync_service.handle_linear_issue_done(done_trigger)

    trigger = extract_review_trigger(
        event,
        await _resolve_actor_id(request, event.organization_id),
        auto_review_enabled=settings.linear_auto_review_enabled,
    )
    if trigger is None:
        response.status_code = status.HTTP_202_ACCEPTED
        return WebhookAck(accepted=True, reason="event_ignored")

    # Attached here rather than inside each extractor: the delivery id is a
    # property of *this HTTP request*, not of the event's meaning, and every
    # extractor would otherwise need the same plumbing.
    trigger = trigger.model_copy(update={"delivery_id": request.headers.get(DELIVERY_HEADER)})

    logger.info(
        "Review trigger received",
        extra={
            "source": trigger.source,
            "linear_issue_id": trigger.issue_id,
            "linear_issue_identifier": trigger.issue_identifier,
            "agent_session_id": trigger.agent_session_id,
            "organization_id": trigger.organization_id,
        },
    )

    # Hand off and return. The review runs on the queue's worker pool, so its
    # lifetime is not tied to this request — and the acknowledgement Linear
    # expects within ten seconds is the worker's first action.
    result = await request.app.state.review_service.submit(trigger)

    response.status_code = status.HTTP_202_ACCEPTED
    return WebhookAck(
        accepted=True,
        queued=result.queued,
        review_id=result.job.id,
        reason=None if result.created else "duplicate_in_flight",
    )


async def _resolve_actor_id(request: Request, organization_id: str | None) -> str | None:
    """Find AIDA-MATE's actor ID for the workspace this event came from.

    Prefers the ID discovered during OAuth install; falls back to the optional
    config override so the issue-assignment path still works before any
    workspace has authorized.
    """
    settings: Settings = request.app.state.settings

    store = getattr(request.app.state, "linear_token_store", None)
    if store is not None:
        installation = (
            await store.get(organization_id) if organization_id else await store.get_default()
        )
        if installation is not None:
            return installation.actor_id

    return settings.aida_mate_linear_actor_id
