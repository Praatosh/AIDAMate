"""Deterministic label derivation.

Turns a risk assessment and a set of areas into the labels published on the
GitHub pull request and mirrored onto the Linear issue. Like the risk engine,
this is pure Python — the model can influence *which areas* were found, never
which labels are applied.

Label vocabulary and formatting follow the flow diagram exactly, including the
space after the colon (`area: auth`, not `area:auth`). The space is part of the
label name as GitHub stores it, so a mismatch silently creates a second,
duplicate label in every repository AIDA-MATE touches.
"""

from collections.abc import Iterable

from app.core.risk_engine import is_owasp_relevant
from app.models.common import AREA_LABELS, Area, RiskLevel, format_label
from app.models.review import RiskAssessment

#: Namespaces where exactly one label may be present at a time. On re-review,
#: stale members are removed before the new one is applied — a pull request must
#: never end up carrying both `risk: low` and `risk: high`.
EXCLUSIVE_NAMESPACES: tuple[str, ...] = ("risk", )

RISK_LABELS: dict[RiskLevel, str] = {
    RiskLevel.LOW: format_label("risk", "low"),
    RiskLevel.MEDIUM: format_label("risk", "medium"),
    RiskLevel.HIGH: format_label("risk", "high"),
}

LABEL_AI_REVIEWED = format_label("review", "ai-reviewed")
LABEL_NEEDS_HUMAN = format_label("review", "needs-human")
LABEL_OWASP = format_label("owasp", "relevant")

#: Colours used when a label has to be created. GitHub requires a hex colour on
#: creation; without a catalog every auto-created label would be the same grey
#: and the risk signal would be lost at a glance.
LABEL_COLORS: dict[str, str] = {
    RISK_LABELS[RiskLevel.LOW]: "0e8a16",      # green
    RISK_LABELS[RiskLevel.MEDIUM]: "fbca04",   # amber
    RISK_LABELS[RiskLevel.HIGH]: "d93f0b",     # red
    LABEL_NEEDS_HUMAN: "b60205",               # dark red
    LABEL_AI_REVIEWED: "5319e7",               # purple
    LABEL_OWASP: "d4c5f9",                     # light purple
}

#: Every `area: *` label shares one colour — they are dimensions, not severities.
AREA_LABEL_COLOR = "1d76db"


def build_labels(assessment: RiskAssessment, areas: Iterable[Area]) -> set[str]:
    """Derive the full label set for a reviewed pull request.

    `review: ai-reviewed` is applied unconditionally on a successful review —
    it is a completion marker. `review: needs-human` is added *on top* when
    escalation is warranted; the two are complementary, not alternatives. A
    reviewer seeing both learns "AIDA-MATE finished, and you should still look".
    """
    area_set = set(areas)

    labels = {RISK_LABELS[assessment.level], LABEL_AI_REVIEWED}

    if assessment.needs_human_review:
        labels.add(LABEL_NEEDS_HUMAN)

    if is_owasp_relevant(area_set):
        labels.add(LABEL_OWASP)

    for area in area_set:
        suffix = AREA_LABELS.get(area)
        if suffix is not None:
            labels.add(format_label("area", suffix))

    return labels


def color_for(label: str) -> str:
    """Return the hex colour to use when creating `label`."""
    return LABEL_COLORS.get(label, AREA_LABEL_COLOR)


def label_catalog(labels: Iterable[str]) -> dict[str, str]:
    """Map each label to its colour, for idempotent creation in GitHub."""
    return {label: color_for(label) for label in labels}
