"""Label derivation — vocabulary, formatting, and exclusivity."""

import pytest

from app.core.label_engine import (
    EXCLUSIVE_NAMESPACES,
    LABEL_AI_REVIEWED,
    LABEL_NEEDS_HUMAN,
    LABEL_OWASP,
    RISK_LABELS,
    build_labels,
    color_for,
    label_catalog,
)
from app.core.risk_engine import assess_risk
from app.models.common import Area, RiskLevel
from app.models.review import RiskAssessment


def _assessment(level: RiskLevel, needs_human: bool = False, score: int = 0) -> RiskAssessment:
    return RiskAssessment(score=score, level=level, needs_human_review=needs_human, breakdown=[])


# --- Formatting matches the diagram -----------------------------------------


def test_labels_carry_the_space_after_the_colon() -> None:
    """GitHub stores the space as part of the name; a mismatch duplicates labels."""
    labels = build_labels(_assessment(RiskLevel.HIGH, True), {Area.AUTHENTICATION})

    assert "risk: high" in labels
    assert "review: needs-human" in labels
    assert "area: auth" in labels
    assert not any(":" in label and ": " not in label for label in labels)


def test_diagram_scenario_produces_the_diagram_labels() -> None:
    """The exact label set shown on the example PR in the flow diagram."""
    labels = build_labels(
        _assessment(RiskLevel.HIGH, needs_human=True), {Area.AUTHENTICATION, Area.API}
    )

    assert labels == {
        "risk: high",
        "review: ai-reviewed",
        "review: needs-human",
        "area: auth",
        "area: api",
        "owasp: relevant",
    }


# --- Risk labels -------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [(RiskLevel.LOW, "risk: low"), (RiskLevel.MEDIUM, "risk: medium"), (RiskLevel.HIGH, "risk: high")],
)
def test_risk_label_per_level(level: RiskLevel, expected: str) -> None:
    assert expected in build_labels(_assessment(level), set())


def test_exactly_one_risk_label() -> None:
    """A PR must never carry both `risk: low` and `risk: high`."""
    for level in RiskLevel:
        labels = build_labels(_assessment(level), {Area.BACKEND, Area.API})
        assert len([label for label in labels if label.startswith("risk: ")]) == 1


def test_risk_namespace_is_marked_exclusive() -> None:
    """Publication removes stale members of exclusive namespaces on re-review."""
    assert "risk" in EXCLUSIVE_NAMESPACES


# --- Review labels -----------------------------------------------------------


def test_ai_reviewed_is_always_applied() -> None:
    """A completion marker: AIDA-MATE finished a pass."""
    for level in RiskLevel:
        assert LABEL_AI_REVIEWED in build_labels(_assessment(level), set())


def test_needs_human_is_additive_not_exclusive() -> None:
    """Both together mean "AI reviewed it, and a person should look too"."""
    labels = build_labels(_assessment(RiskLevel.HIGH, needs_human=True), set())

    assert LABEL_AI_REVIEWED in labels
    assert LABEL_NEEDS_HUMAN in labels


def test_needs_human_absent_when_not_required() -> None:
    assert LABEL_NEEDS_HUMAN not in build_labels(_assessment(RiskLevel.LOW), set())


# --- Area labels -------------------------------------------------------------


def test_multiple_areas_produce_multiple_labels() -> None:
    labels = build_labels(_assessment(RiskLevel.MEDIUM), {Area.BACKEND, Area.DATABASE})

    assert "area: backend" in labels
    assert "area: db" in labels


def test_related_areas_collapse_to_one_label() -> None:
    """Distinct for scoring, identical to a reader."""
    labels = build_labels(_assessment(RiskLevel.HIGH), {Area.AUTHENTICATION, Area.AUTHORIZATION})

    assert len([label for label in labels if label.startswith("area: ")]) == 1
    assert "area: auth" in labels


def test_vague_areas_produce_no_label() -> None:
    labels = build_labels(_assessment(RiskLevel.MEDIUM), {Area.BUSINESS_LOGIC, Area.CONFIGURATION})

    assert not any(label.startswith("area: ") for label in labels)


# --- OWASP -------------------------------------------------------------------


@pytest.mark.parametrize("area", [Area.AUTHENTICATION, Area.SECURITY, Area.DEPENDENCIES])
def test_owasp_label_applied_for_relevant_areas(area: Area) -> None:
    assert LABEL_OWASP in build_labels(_assessment(RiskLevel.MEDIUM), {area})


def test_owasp_label_absent_otherwise() -> None:
    assert LABEL_OWASP not in build_labels(_assessment(RiskLevel.LOW), {Area.DOCUMENTATION})


# --- Colours -----------------------------------------------------------------


def test_risk_levels_have_distinct_colours() -> None:
    colours = {color_for(RISK_LABELS[level]) for level in RiskLevel}

    assert len(colours) == 3


def test_area_labels_share_a_colour() -> None:
    """Areas are dimensions, not severities — they should not look alarming."""
    assert color_for("area: auth") == color_for("area: frontend")


def test_catalog_covers_every_label() -> None:
    labels = build_labels(_assessment(RiskLevel.HIGH, True), {Area.AUTHENTICATION, Area.FRONTEND})
    catalog = label_catalog(labels)

    assert set(catalog) == labels
    assert all(len(colour) == 6 for colour in catalog.values())


# --- Integration with the risk engine ---------------------------------------


def test_end_to_end_low_risk_documentation_pr() -> None:
    assessment = assess_risk({Area.DOCUMENTATION})
    labels = build_labels(assessment, {Area.DOCUMENTATION})

    assert labels == {"risk: low", "review: ai-reviewed", "area: docs"}


def test_end_to_end_high_risk_auth_pr() -> None:
    areas = {Area.AUTHENTICATION, Area.API, Area.MIGRATIONS}
    assessment = assess_risk(areas)
    labels = build_labels(assessment, areas)

    assert assessment.level is RiskLevel.HIGH
    assert "risk: high" in labels
    assert LABEL_NEEDS_HUMAN in labels
    assert LABEL_OWASP in labels
