"""Automatic review triggering and label formatting."""

import pytest

from app.api.linear_webhook import extract_review_trigger
from app.core.config import Settings
from app.models.common import AREA_LABELS, Area, format_label
from app.models.linear import LinearWebhookEvent

ACTOR = "aida-mate-actor-id"


def _issue_event(**overrides) -> LinearWebhookEvent:
    payload = {
        "type": "Issue",
        "action": "update",
        "organizationId": "org-1",
        "data": {"id": "issue-1", "identifier": "MATE-123", "assigneeId": "some-human"},
        "updatedFrom": {"title": "old title"},
    }
    payload.update(overrides)
    return LinearWebhookEvent.model_validate(payload)


# --- Automatic mode ---------------------------------------------------------


def test_auto_mode_off_ignores_plain_edits() -> None:
    """The default: a ticket edit costs nothing."""
    assert extract_review_trigger(_issue_event(), ACTOR, auto_review_enabled=False) is None


def test_auto_mode_on_triggers_on_update() -> None:
    trigger = extract_review_trigger(_issue_event(), ACTOR, auto_review_enabled=True)

    assert trigger is not None
    assert trigger.source == "issue_auto"
    assert trigger.issue_id == "issue-1"


def test_auto_mode_triggers_on_create() -> None:
    event = _issue_event(action="create", updatedFrom={})

    assert extract_review_trigger(event, ACTOR, auto_review_enabled=True) is not None


def test_auto_mode_ignores_removals() -> None:
    event = _issue_event(action="remove")

    assert extract_review_trigger(event, ACTOR, auto_review_enabled=True) is None


def test_assignment_takes_precedence_over_auto() -> None:
    """A deliberate request must be attributed to the path the requester chose."""
    event = _issue_event(
        data={"id": "issue-1", "assigneeId": ACTOR}, updatedFrom={"assigneeId": "human"}
    )

    trigger = extract_review_trigger(event, ACTOR, auto_review_enabled=True)

    assert trigger.source == "issue_assignment"


def test_agent_session_still_wins_in_auto_mode() -> None:
    event = LinearWebhookEvent.model_validate(
        {
            "type": "AgentSessionEvent",
            "action": "created",
            "data": {"agentSession": {"id": "s-1", "issue": {"id": "i-1"}}},
        }
    )

    trigger = extract_review_trigger(event, ACTOR, auto_review_enabled=True)

    assert trigger.source == "agent_session"
    assert trigger.agent_session_id == "s-1"


def test_auto_mode_needs_an_issue_id() -> None:
    event = _issue_event(data={"identifier": "MATE-1"})

    assert extract_review_trigger(event, ACTOR, auto_review_enabled=True) is None


def test_auto_mode_ignores_non_issue_entities() -> None:
    event = _issue_event(type="Project")

    assert extract_review_trigger(event, ACTOR, auto_review_enabled=True) is None


def test_auto_review_is_off_by_default() -> None:
    assert Settings(_env_file=None).linear_auto_review_enabled is False


# --- Label formatting -------------------------------------------------------


def test_labels_render_with_a_space() -> None:
    """The space is part of the label name; a mismatch duplicates every label."""
    assert format_label("area", "auth") == "area: auth"
    assert format_label("risk", "high") == "risk: high"
    assert format_label("review", "needs-human") == "review: needs-human"


def test_area_labels_match_the_published_vocabulary() -> None:
    published = set(AREA_LABELS.values())

    assert published == {
        "auth",
        "security",
        "backend",
        "frontend",
        "api",
        "db",
        "infra",
        "payments",
        "deps",
        "tests",
        "docs",
        "ai",
    }


def test_related_areas_collapse_onto_one_label() -> None:
    """Distinct for scoring, identical to a human reader."""
    assert AREA_LABELS[Area.AUTHENTICATION] == AREA_LABELS[Area.AUTHORIZATION] == "auth"
    assert AREA_LABELS[Area.DATABASE] == AREA_LABELS[Area.MIGRATIONS] == "db"
    assert AREA_LABELS[Area.INFRASTRUCTURE] == AREA_LABELS[Area.CI_CD] == "infra"


def test_vague_areas_produce_no_label() -> None:
    """`business-logic` on a PR tells a reviewer nothing."""
    assert Area.BUSINESS_LOGIC not in AREA_LABELS
    assert Area.CONFIGURATION not in AREA_LABELS


@pytest.mark.parametrize("area", list(Area))
def test_every_area_label_is_lowercase_and_spaceless(area: Area) -> None:
    suffix = AREA_LABELS.get(area)
    if suffix is None:
        return
    assert suffix == suffix.lower()
    assert " " not in suffix


def test_ai_area_exists() -> None:
    """Present in the flow diagram, and relevant to a tool reviewing AI code."""
    assert AREA_LABELS[Area.AI] == "ai"


# --- Repo allowlist ---------------------------------------------------------


def test_repo_allowlist_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPO_ALLOWLIST", " acme/api , acme/web ,, bad-entry , acme/api ")

    repos = Settings(_env_file=None).github_repos

    assert repos == ["acme/api", "acme/web"]


def test_empty_allowlist_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # `dummy_env` (conftest.py) sets GITHUB_REPO_ALLOWLIST=acme/api for the
    # rest of the suite's benefit — cleared here to test the actual default.
    monkeypatch.setenv("GITHUB_REPO_ALLOWLIST", "")

    assert Settings(_env_file=None).github_repos == []
