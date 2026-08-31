"""Deterministic risk scoring.

The hardest constraint in the specification: *the LLM must never decide the
final risk*. This module is the only thing that assigns LOW / MEDIUM / HIGH.
It is pure Python, has no network or model dependency, and is fully
reproducible — the same pull request always scores identically.

**Scoring is per distinct area, counted once.** The specification's table
(`Authentication +50`, `Database migration +40`, `Terraform +30`, `Frontend
+5`, `README +1`) describes *what a pull request touches*, not how many
observations were made about it. Counting per finding instead would hand the
model indirect control over risk magnitude: an agent that emitted ten backend
findings would score 150 where a terser one scored 15, for the same diff. That
is exactly the influence the specification forbids, arriving through the back
door — so findings contribute their *category* to the area set and nothing more.

Severity is likewise never multiplied into the score. It is reported to humans
and it can force human review (see `needs_human_review`), but it cannot move
the number.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.models.common import Area, RiskLevel, Severity
from app.models.review import Finding, RiskAssessment, ScoreContribution

#: Points per area. Values from the specification are marked; the rest are
#: calibrated onto the same scale and are configuration, not logic — edit this
#: table to retune, no code changes required.
AREA_POINTS: dict[Area, int] = {
    Area.AUTHENTICATION: 50,      # spec
    Area.AUTHORIZATION: 50,       # spec
    Area.SECURITY: 40,            # spec
    Area.MIGRATIONS: 40,          # spec ("database migration")
    Area.PAYMENTS: 40,            # spec
    Area.INFRASTRUCTURE: 30,      # spec ("terraform")
    Area.API: 30,                 # spec ("public API")
    Area.DATABASE: 25,
    Area.CI_CD: 20,
    Area.BUSINESS_LOGIC: 20,      # spec
    Area.AI: 20,
    Area.BACKEND: 15,             # spec
    Area.CONFIGURATION: 10,
    Area.DEPENDENCIES: 10,
    Area.FRONTEND: 5,             # spec
    Area.TESTING: 3,
    Area.DOCUMENTATION: 1,        # spec
}

#: Points for an area with no entry above. Non-zero so an unrecognised area
#: cannot silently vanish from the score, but small enough that one alone
#: cannot reach MEDIUM.
UNKNOWN_AREA_POINTS = 10

#: Severities that force human review regardless of the computed level. A
#: critical finding on an otherwise-LOW pull request still deserves a person.
_ESCALATING_SEVERITIES = frozenset({Severity.HIGH, Severity.CRITICAL})

#: Areas that map onto the OWASP Top 10. Membership, not model judgement, so
#: the label is as reproducible as the score.
OWASP_RELEVANT_AREAS = frozenset(
    {Area.AUTHENTICATION, Area.AUTHORIZATION, Area.SECURITY, Area.API, Area.DEPENDENCIES}
)


@dataclass(frozen=True)
class RiskThresholds:
    """Bucket boundaries and the human-review policy.

    Sourced from settings so they can be retuned per deployment without a code
    change, as the specification requires.
    """

    low_max: int = 20
    medium_max: int = 60
    medium_requires_human_review: bool = False

    @classmethod
    def from_settings(cls, settings) -> "RiskThresholds":
        """Build thresholds from application settings."""
        return cls(
            low_max=settings.risk_low_max_score,
            medium_max=settings.risk_medium_max_score,
            medium_requires_human_review=settings.medium_requires_human_review,
        )

    def bucket(self, score: int) -> RiskLevel:
        """Map a score onto a risk level."""
        if score <= self.low_max:
            return RiskLevel.LOW
        if score <= self.medium_max:
            return RiskLevel.MEDIUM
        return RiskLevel.HIGH


def points_for(area: Area) -> int:
    """Return the configured points for an area."""
    return AREA_POINTS.get(area, UNKNOWN_AREA_POINTS)


def is_owasp_relevant(areas: Iterable[Area]) -> bool:
    """Whether any affected area maps onto the OWASP Top 10."""
    return any(area in OWASP_RELEVANT_AREAS for area in areas)


def assess_risk(
    areas: Iterable[Area],
    *,
    findings: Sequence[Finding] = (),
    thresholds: RiskThresholds | None = None,
) -> RiskAssessment:
    """Compute the risk level for a pull request.

    Args:
        areas: Areas the pull request touches, typically from
            `area_detector.detect_areas`.
        findings: Optional model-produced findings. Their *categories* join the
            area set and their *severities* can force human review; neither
            changes how points are calculated.
        thresholds: Bucket boundaries. Defaults to the specification's values.

    Returns:
        A `RiskAssessment` whose `breakdown` explains every point, so a HIGH
        classification can always be justified to the developer who receives it.
    """
    policy = thresholds or RiskThresholds()

    # Union, so a finding about an area already detected from paths does not
    # score twice.
    effective_areas = set(areas) | {finding.category for finding in findings}

    breakdown = [
        ScoreContribution(area=area, points=points_for(area), rule=_rule_name(area))
        for area in sorted(effective_areas, key=lambda a: (-points_for(a), a.value))
    ]
    score = sum(contribution.points for contribution in breakdown)
    level = policy.bucket(score)

    return RiskAssessment(
        score=score,
        level=level,
        needs_human_review=needs_human_review(level, findings, policy),
        breakdown=breakdown,
    )


def needs_human_review(
    level: RiskLevel,
    findings: Sequence[Finding] = (),
    thresholds: RiskThresholds | None = None,
) -> bool:
    """Decide whether a person must look at this pull request.

    HIGH always requires review and this can never be turned off — the
    specification is explicit that human review must not be silently bypassed
    for high-risk changes. MEDIUM is configurable. LOW does not require review
    unless a finding is severe enough to warrant it on its own.
    """
    policy = thresholds or RiskThresholds()

    if level is RiskLevel.HIGH:
        return True
    if level is RiskLevel.MEDIUM and policy.medium_requires_human_review:
        return True
    return any(finding.severity in _ESCALATING_SEVERITIES for finding in findings)


def _rule_name(area: Area) -> str:
    """Human-readable name of the rule that contributed points."""
    if area in AREA_POINTS:
        return f"{area.value} touched"
    return f"{area.value} touched (unrecognised area, default weight)"
