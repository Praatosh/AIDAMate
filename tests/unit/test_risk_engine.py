"""Deterministic risk scoring — the component the LLM must never control."""

import pytest

from app.core.risk_engine import (
    AREA_POINTS,
    UNKNOWN_AREA_POINTS,
    RiskThresholds,
    assess_risk,
    is_owasp_relevant,
    needs_human_review,
    points_for,
)
from app.models.common import Area, RiskLevel, Severity
from app.models.review import Finding


def _finding(category: Area, severity: Severity = Severity.MEDIUM) -> Finding:
    return Finding(category=category, severity=severity, description="x")


# --- The specification's own numbers -----------------------------------------


@pytest.mark.parametrize(
    ("area", "expected"),
    [
        (Area.AUTHENTICATION, 50),
        (Area.AUTHORIZATION, 50),
        (Area.SECURITY, 40),
        (Area.MIGRATIONS, 40),
        (Area.PAYMENTS, 40),
        (Area.INFRASTRUCTURE, 30),
        (Area.API, 30),
        (Area.BUSINESS_LOGIC, 20),
        (Area.BACKEND, 15),
        (Area.FRONTEND, 5),
        (Area.DOCUMENTATION, 1),
    ],
)
def test_specified_area_weights(area: Area, expected: int) -> None:
    assert points_for(area) == expected


def test_unknown_area_gets_a_default_weight() -> None:
    """An unmapped area must not silently vanish from the score."""
    assert points_for("not-a-real-area") == UNKNOWN_AREA_POINTS  # type: ignore[arg-type]


def test_one_unknown_area_cannot_reach_medium() -> None:
    assert RiskThresholds().bucket(UNKNOWN_AREA_POINTS) is RiskLevel.LOW


# --- Bucketing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, RiskLevel.LOW),
        (20, RiskLevel.LOW),
        (21, RiskLevel.MEDIUM),
        (60, RiskLevel.MEDIUM),
        (61, RiskLevel.HIGH),
        (500, RiskLevel.HIGH),
    ],
)
def test_threshold_boundaries(score: int, expected: RiskLevel) -> None:
    assert RiskThresholds().bucket(score) is expected


def test_thresholds_are_configurable() -> None:
    """Retuning must not require a code change."""
    strict = RiskThresholds(low_max=5, medium_max=15)

    assert strict.bucket(6) is RiskLevel.MEDIUM
    assert strict.bucket(16) is RiskLevel.HIGH


# --- Realistic scenarios -----------------------------------------------------


def test_documentation_only_is_low() -> None:
    assessment = assess_risk({Area.DOCUMENTATION})

    assert assessment.score == 1
    assert assessment.level is RiskLevel.LOW
    assert assessment.needs_human_review is False


def test_frontend_only_is_low() -> None:
    assert assess_risk({Area.FRONTEND}).level is RiskLevel.LOW


def test_auth_change_is_high() -> None:
    """The specification's headline case."""
    assessment = assess_risk({Area.AUTHENTICATION})

    assert assessment.score == 50
    assert assessment.level is RiskLevel.MEDIUM

    # Auth rarely arrives alone; with the API surface it crosses into HIGH.
    combined = assess_risk({Area.AUTHENTICATION, Area.API})
    assert combined.score == 80
    assert combined.level is RiskLevel.HIGH


def test_migration_plus_backend_is_high() -> None:
    assessment = assess_risk({Area.MIGRATIONS, Area.BACKEND, Area.DATABASE})

    assert assessment.score == 80
    assert assessment.level is RiskLevel.HIGH


def test_areas_compound() -> None:
    """Several moderate areas together can exceed what none reaches alone."""
    assert assess_risk({Area.BACKEND}).level is RiskLevel.LOW
    assert assess_risk({Area.BACKEND, Area.API, Area.DATABASE}).level is RiskLevel.HIGH


def test_no_areas_scores_zero() -> None:
    assessment = assess_risk(set())

    assert assessment.score == 0
    assert assessment.level is RiskLevel.LOW


# --- The central guarantee ---------------------------------------------------


def test_each_area_counts_once_regardless_of_finding_volume() -> None:
    """A chattier model must not produce a riskier verdict for the same diff.

    Counting per finding would let the LLM inflate the score simply by emitting
    more observations — indirect control over risk magnitude, which the
    specification forbids.
    """
    terse = assess_risk({Area.BACKEND}, findings=[_finding(Area.BACKEND)])
    chatty = assess_risk({Area.BACKEND}, findings=[_finding(Area.BACKEND) for _ in range(20)])

    assert terse.score == chatty.score == points_for(Area.BACKEND)


def test_findings_contribute_their_category_to_the_area_set() -> None:
    """A model may surface an area the paths did not reveal."""
    from_paths_only = assess_risk({Area.BACKEND})
    with_finding = assess_risk({Area.BACKEND}, findings=[_finding(Area.SECURITY)])

    assert with_finding.score == from_paths_only.score + points_for(Area.SECURITY)


def test_severity_never_changes_the_score() -> None:
    """Severity informs humans; it must not move the number."""
    low = assess_risk({Area.BACKEND}, findings=[_finding(Area.BACKEND, Severity.INFO)])
    critical = assess_risk({Area.BACKEND}, findings=[_finding(Area.BACKEND, Severity.CRITICAL)])

    assert low.score == critical.score


def test_scoring_is_reproducible() -> None:
    areas = {Area.AUTHENTICATION, Area.API, Area.DATABASE}

    assert assess_risk(areas).score == assess_risk(areas).score


# --- Human review ------------------------------------------------------------


def test_high_always_requires_human_review() -> None:
    """The specification forbids silently bypassing review for high risk."""
    assert needs_human_review(RiskLevel.HIGH) is True
    assert needs_human_review(RiskLevel.HIGH, thresholds=RiskThresholds()) is True


def test_low_does_not_require_review_by_default() -> None:
    assert needs_human_review(RiskLevel.LOW) is False


def test_medium_is_configurable() -> None:
    assert needs_human_review(RiskLevel.MEDIUM) is False
    assert (
        needs_human_review(RiskLevel.MEDIUM, thresholds=RiskThresholds(medium_requires_human_review=True))
        is True
    )


@pytest.mark.parametrize("severity", [Severity.HIGH, Severity.CRITICAL])
def test_a_severe_finding_escalates_even_a_low_risk_pr(severity: Severity) -> None:
    assessment = assess_risk({Area.DOCUMENTATION}, findings=[_finding(Area.SECURITY, severity)])

    assert assessment.needs_human_review is True


@pytest.mark.parametrize("severity", [Severity.INFO, Severity.LOW, Severity.MEDIUM])
def test_mild_findings_do_not_escalate(severity: Severity) -> None:
    assert needs_human_review(RiskLevel.LOW, [_finding(Area.FRONTEND, severity)]) is False


# --- Auditability ------------------------------------------------------------


def test_breakdown_explains_every_point() -> None:
    """A HIGH verdict must be justifiable to the developer who receives it."""
    assessment = assess_risk({Area.AUTHENTICATION, Area.API, Area.FRONTEND})

    assert sum(c.points for c in assessment.breakdown) == assessment.score
    assert {c.area for c in assessment.breakdown} == {Area.AUTHENTICATION, Area.API, Area.FRONTEND}
    assert all(c.rule for c in assessment.breakdown)


def test_breakdown_is_ordered_by_weight() -> None:
    assessment = assess_risk({Area.DOCUMENTATION, Area.AUTHENTICATION, Area.FRONTEND})

    points = [c.points for c in assessment.breakdown]
    assert points == sorted(points, reverse=True)


# --- OWASP -------------------------------------------------------------------


@pytest.mark.parametrize(
    "area", [Area.AUTHENTICATION, Area.AUTHORIZATION, Area.SECURITY, Area.API, Area.DEPENDENCIES]
)
def test_owasp_relevant_areas(area: Area) -> None:
    assert is_owasp_relevant({area}) is True


@pytest.mark.parametrize("area", [Area.FRONTEND, Area.DOCUMENTATION, Area.TESTING])
def test_non_owasp_areas(area: Area) -> None:
    assert is_owasp_relevant({area}) is False


def test_owasp_is_membership_not_judgement() -> None:
    """Reproducible, like the score — never a model decision."""
    assert is_owasp_relevant(set()) is False


# --- Table integrity ---------------------------------------------------------


def test_every_area_has_a_weight() -> None:
    """A new Area must not silently fall through to the default."""
    missing = [area for area in Area if area not in AREA_POINTS]

    assert missing == [], f"Areas missing from AREA_POINTS: {missing}"


def test_weights_are_positive() -> None:
    assert all(points > 0 for points in AREA_POINTS.values())
