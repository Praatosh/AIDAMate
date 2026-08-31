"""Webhook signature verification, replay protection, and trigger extraction.

The security boundary of the whole system: a forged or replayed delivery must
never start a review, and a review must fire exactly when AIDA-MATE is delegated
or assigned an issue — not on every subsequent edit to it.
"""

import hashlib
import hmac
import time

import pytest

from app.api.linear_webhook import (
    _extract_issue_done_trigger,
    extract_review_trigger,
    is_fresh,
    verify_signature,
)
from app.models.linear import LinearWebhookEvent

SECRET = "test-linear-webhook-secret"
ACTOR = "aida-mate-actor-id"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_valid_signature_accepted() -> None:
    body = b'{"type":"Issue"}'

    assert verify_signature(body, _sig(body), SECRET) is True


def test_signature_from_wrong_secret_rejected() -> None:
    body = b'{"type":"Issue"}'

    assert verify_signature(body, _sig(body, "attacker-secret"), SECRET) is False


@pytest.mark.parametrize("header", [None, "", "not-a-hex-digest", "0" * 64])
def test_absent_or_bogus_signatures_rejected(header: str | None) -> None:
    assert verify_signature(b"{}", header, SECRET) is False


def test_signature_is_body_specific() -> None:
    """A signature valid for one body must not validate a different body."""
    assert verify_signature(b'{"amount":9}', _sig(b'{"amount":1}'), SECRET) is False


def test_empty_secret_fails_closed() -> None:
    """An unset secret must reject everything, never accept everything."""
    assert verify_signature(b"{}", _sig(b"{}", ""), "") is False


def test_github_style_prefixed_signature_rejected() -> None:
    """Linear sends a bare hex digest; GitHub's `sha256=` prefix is not valid here."""
    body = b"{}"

    assert verify_signature(body, f"sha256={_sig(body)}", SECRET) is False


def test_surrounding_whitespace_tolerated() -> None:
    body = b"{}"

    assert verify_signature(body, f"  {_sig(body)}  ", SECRET) is True


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_recent_delivery_is_fresh() -> None:
    assert is_fresh(_now_ms(), 60) is True


def test_old_delivery_is_stale() -> None:
    """A captured request stays signature-valid forever; age is the only defense."""
    assert is_fresh(_now_ms() - 120_000, 60) is False


def test_future_delivery_is_stale() -> None:
    """Clock skew is checked in both directions."""
    assert is_fresh(_now_ms() + 120_000, 60) is False


def test_missing_timestamp_is_treated_as_fresh() -> None:
    """Some Linear event types omit it; rejecting those would break real traffic."""
    assert is_fresh(None, 60) is True


def test_freshness_boundary() -> None:
    now = _now_ms()

    assert is_fresh(now - 60_000, 60, now_ms=now) is True
    assert is_fresh(now - 60_001, 60, now_ms=now) is False


# ---------------------------------------------------------------------------
# AgentSessionEvent — the primary agent trigger
# ---------------------------------------------------------------------------


def _agent_event(**overrides) -> LinearWebhookEvent:
    payload = {
        "type": "AgentSessionEvent",
        "action": "created",
        "organizationId": "org-1",
        "webhookId": "wh-1",
        "data": {
            "agentSession": {
                "id": "session-1",
                "promptContext": "Issue ENG-1: Harden login",
                "issue": {"id": "issue-1", "identifier": "ENG-1", "title": "Harden login"},
            }
        },
    }
    payload.update(overrides)
    return LinearWebhookEvent.model_validate(payload)


def test_agent_session_created_triggers_review() -> None:
    trigger = extract_review_trigger(_agent_event(), ACTOR)

    assert trigger is not None
    assert trigger.source == "agent_session"
    assert trigger.issue_id == "issue-1"
    assert trigger.issue_identifier == "ENG-1"
    assert trigger.agent_session_id == "session-1"
    assert trigger.prompt_context == "Issue ENG-1: Harden login"
    assert trigger.organization_id == "org-1"


def test_agent_session_trigger_needs_no_configured_actor_id() -> None:
    """Linear only sends these to the app they belong to, so no matching is required."""
    assert extract_review_trigger(_agent_event(), None) is not None


def test_flat_agent_session_payload_also_parsed() -> None:
    """Nesting is not pinned down by public docs, so both shapes are accepted."""
    event = _agent_event(
        data={
            "id": "session-1",
            "promptContext": "ctx",
            "issue": {"id": "issue-1", "identifier": "ENG-1"},
        }
    )
    trigger = extract_review_trigger(event, ACTOR)

    assert trigger is not None
    assert trigger.agent_session_id == "session-1"
    assert trigger.issue_id == "issue-1"


def test_prompt_context_found_at_top_level_of_data() -> None:
    event = _agent_event(
        data={
            "promptContext": "top-level ctx",
            "agentSession": {"id": "s-1", "issue": {"id": "issue-1"}},
        }
    )

    assert extract_review_trigger(event, ACTOR).prompt_context == "top-level ctx"


def test_agent_session_without_an_issue_is_ignored() -> None:
    """Nothing to review without an issue; better to ignore than to guess."""
    event = _agent_event(data={"agentSession": {"id": "s-1"}})

    assert extract_review_trigger(event, ACTOR) is None


def test_prompted_action_does_not_start_a_new_review() -> None:
    """`prompted` is a follow-up message in an existing session, handled later."""
    assert extract_review_trigger(_agent_event(action="prompted"), ACTOR) is None


# ---------------------------------------------------------------------------
# Issue assignment — the secondary trigger
# ---------------------------------------------------------------------------


def _issue_event(**overrides) -> LinearWebhookEvent:
    payload = {
        "type": "Issue",
        "action": "update",
        "organizationId": "org-1",
        "data": {"id": "issue-1", "identifier": "ENG-1", "assigneeId": ACTOR},
        "updatedFrom": {"assigneeId": "human-user"},
    }
    payload.update(overrides)
    return LinearWebhookEvent.model_validate(payload)


def test_assignment_to_aida_mate_triggers_review() -> None:
    trigger = extract_review_trigger(_issue_event(), ACTOR)

    assert trigger is not None
    assert trigger.source == "issue_assignment"
    assert trigger.issue_id == "issue-1"
    assert trigger.agent_session_id is None


def test_assignment_to_another_user_ignored() -> None:
    event = _issue_event(data={"id": "issue-1", "assigneeId": "some-human"})

    assert extract_review_trigger(event, ACTOR) is None


def test_unrelated_edit_to_already_assigned_issue_ignored() -> None:
    """The critical case: assignee unchanged, so `updatedFrom` lacks assigneeId.

    Without this check AIDA-MATE would re-review on every later title or
    description edit of an issue already assigned to it.
    """
    assert extract_review_trigger(_issue_event(updatedFrom={"title": "old"}), ACTOR) is None


def test_missing_updated_from_ignored() -> None:
    assert extract_review_trigger(_issue_event(updatedFrom={}), ACTOR) is None


def test_assignment_ignored_when_actor_id_unknown() -> None:
    """Without knowing our own ID we cannot tell if the assignment was to us."""
    assert extract_review_trigger(_issue_event(), None) is None


def test_remove_action_is_ignored() -> None:
    assert extract_review_trigger(_issue_event(action="remove"), ACTOR) is None


def test_created_already_assigned_to_aida_mate_triggers_review() -> None:
    """A new issue can be created pre-assigned via Linear's creation dialog —
    there is no `updatedFrom` to check for a brand-new record, so the field's
    mere presence is the signal."""
    event = _issue_event(action="create", data={"id": "issue-1", "assigneeId": ACTOR})

    trigger = extract_review_trigger(event, ACTOR)

    assert trigger is not None
    assert trigger.source == "issue_assignment"


def test_created_assigned_to_someone_else_is_ignored() -> None:
    event = _issue_event(action="create", data={"id": "issue-1", "assigneeId": "some-human"})

    assert extract_review_trigger(event, ACTOR) is None


def test_created_already_delegated_to_aida_mate_triggers_review() -> None:
    event = _issue_event(action="create", data={"id": "issue-1", "delegateId": ACTOR})

    trigger = extract_review_trigger(event, ACTOR)

    assert trigger is not None
    assert trigger.source == "issue_assignment"


# ---------------------------------------------------------------------------
# Issue delegation — Linear's "Delegate" UI action for app actors
#
# Confirmed against live webhook traffic: this action sends a plain `Issue`
# `update` event with `delegateId` changing (and `assigneeId` cleared, not
# set) — not an `AgentSessionEvent`, despite being the delegation flow.
# ---------------------------------------------------------------------------


def test_delegation_to_aida_mate_triggers_review() -> None:
    event = _issue_event(
        data={"id": "issue-1", "identifier": "ENG-1", "delegateId": ACTOR},
        updatedFrom={"subscriberIds": ["human-user"], "delegateId": None},
    )

    trigger = extract_review_trigger(event, ACTOR)

    assert trigger is not None
    assert trigger.source == "issue_assignment"
    assert trigger.issue_id == "issue-1"


def test_delegation_to_another_actor_ignored() -> None:
    event = _issue_event(
        data={"id": "issue-1", "delegateId": "some-other-app"},
        updatedFrom={"delegateId": None},
    )

    assert extract_review_trigger(event, ACTOR) is None


def test_unrelated_edit_to_delegated_issue_ignored() -> None:
    """delegateId unchanged, so `updatedFrom` lacks it — must not re-trigger."""
    event = _issue_event(
        data={"id": "issue-1", "delegateId": ACTOR},
        updatedFrom={"title": "old"},
    )

    assert extract_review_trigger(event, ACTOR) is None


def test_missing_issue_id_ignored() -> None:
    assert extract_review_trigger(_issue_event(data={"assigneeId": ACTOR}), ACTOR) is None


@pytest.mark.parametrize("entity_type", ["Comment", "Project", "Cycle", "Reaction"])
def test_unrelated_entity_types_ignored(entity_type: str) -> None:
    assert extract_review_trigger(_issue_event(type=entity_type), ACTOR) is None


def test_malformed_data_does_not_raise() -> None:
    """Hostile or malformed payloads must be ignored, never crash the handler."""
    for bad in (None, "a string", 42, []):
        event = LinearWebhookEvent.model_validate(
            {"type": "AgentSessionEvent", "action": "created", "data": bad}
        )
        assert extract_review_trigger(event, ACTOR) is None


# ---------------------------------------------------------------------------
# Issue Done transition — the gated auto-merge trigger (CLAUDE.md §1a)
# ---------------------------------------------------------------------------


def _state_event(**overrides) -> LinearWebhookEvent:
    payload = {
        "type": "Issue",
        "action": "update",
        "organizationId": "org-1",
        "data": {
            "id": "issue-1",
            "identifier": "ENG-1",
            "state": {"id": "state-done", "name": "Done", "type": "completed"},
        },
        "updatedFrom": {"stateId": "state-in-progress"},
    }
    payload.update(overrides)
    return LinearWebhookEvent.model_validate(payload)


def test_state_change_to_completed_triggers_done() -> None:
    trigger = _extract_issue_done_trigger(_state_event())

    assert trigger is not None
    assert trigger.source == "issue_done"
    assert trigger.issue_id == "issue-1"
    assert trigger.issue_identifier == "ENG-1"


def test_state_change_to_non_completed_type_ignored() -> None:
    event = _state_event(data={"id": "issue-1", "state": {"type": "started"}})

    assert _extract_issue_done_trigger(event) is None


def test_unrelated_edit_without_state_change_ignored() -> None:
    """stateId unchanged, so `updatedFrom` lacks it — must not fire on every edit."""
    assert _extract_issue_done_trigger(_state_event(updatedFrom={"title": "old"})) is None


def test_create_action_never_triggers_done() -> None:
    """A brand-new issue cannot already be "done" the way delegation can already be assigned."""
    event = _state_event(action="create")

    assert _extract_issue_done_trigger(event) is None


def test_done_trigger_missing_issue_id_ignored() -> None:
    event = _state_event(data={"state": {"type": "completed"}})

    assert _extract_issue_done_trigger(event) is None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_endpoint_rejects_invalid_signature(client) -> None:
    response = client.post(
        "/webhooks/linear",
        content=b'{"type":"Issue"}',
        headers={"Linear-Signature": "deadbeef", "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["reason"] == "invalid_signature"


def test_endpoint_rejects_unsigned_request(client) -> None:
    assert client.post("/webhooks/linear", json={"type": "Issue"}).status_code == 401


def test_endpoint_accepts_agent_session_trigger(signed_post) -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "created",
        "webhookTimestamp": _now_ms(),
        "data": {"agentSession": {"id": "s-1", "issue": {"id": "i-1", "identifier": "ENG-1"}}},
    }
    response = signed_post("/webhooks/linear", payload)

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["queued"] is True
    assert body["review_id"]


def test_endpoint_rejects_stale_delivery(signed_post) -> None:
    payload = {
        "type": "AgentSessionEvent",
        "action": "created",
        "webhookTimestamp": _now_ms() - 300_000,
        "data": {"agentSession": {"id": "s-1", "issue": {"id": "i-1"}}},
    }
    response = signed_post("/webhooks/linear", payload)

    assert response.status_code == 400
    assert response.json()["reason"] == "stale_delivery"


def test_endpoint_accepts_and_ignores_unrelated_event(signed_post) -> None:
    """Unrelated events get 2xx — a 4xx would trigger pointless Linear retries."""
    response = signed_post("/webhooks/linear", {"type": "Comment", "action": "create", "data": {}})

    assert response.status_code == 202
    assert response.json()["reason"] == "event_ignored"


def test_endpoint_rejects_unparseable_body(client) -> None:
    body = b"this is not json"
    response = client.post(
        "/webhooks/linear",
        content=body,
        headers={"Linear-Signature": _sig(body), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_payload"
