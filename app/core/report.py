"""Rendering a review result for humans.

Pure formatting — no I/O, so it is fully testable and the exact text posted to
GitHub and Linear can be asserted.

Two audiences, two renderings. The GitHub comment sits next to the diff and
carries the detail a reviewer needs; the Linear comment is a status update for
whoever asked for the review and stays short, with a link back to the PR.

Both are deliberately compact. The specification is explicit that enormous
model output must not be posted: a wall of text gets ignored, and the point is
to make the verdict and its justification obvious at a glance.
"""

from app.core.risk_engine import OWASP_RELEVANT_AREAS
from app.models.common import RiskLevel
from app.models.review import Finding, ReviewResult

#: Hidden marker used to find AIDA-MATE's own comment on re-review, so the
#: existing one is edited rather than a duplicate posted on every push.
GITHUB_COMMENT_MARKER = "<!-- aida-mate-review -->"

#: GitHub rejects comment bodies over 65536 characters. Leaving headroom keeps
#: a long findings list from failing the whole publication step.
MAX_COMMENT_LENGTH = 60_000

#: Cap on findings rendered individually; the rest are summarised as a count.
MAX_FINDINGS_RENDERED = 25

_RISK_EMOJI = {RiskLevel.LOW: "🟢", RiskLevel.MEDIUM: "🟡", RiskLevel.HIGH: "🔴"}

_AI_NOT_RUN = "No (deterministic only — sandbox unavailable)"


def _truncate(text: str, limit: int = MAX_COMMENT_LENGTH) -> str:
    """Trim to `limit`, marking the cut so nothing looks silently lost."""
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n\n_…truncated by AIDA-MATE._"


def _areas_line(result: ReviewResult) -> str:
    """Comma-separated area list, or an explicit 'none'."""
    return ", ".join(area.value for area in result.areas) or "none detected"


def _split_findings(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (vulnerabilities, issues).

    Reuses `OWASP_RELEVANT_AREAS` — the same deterministic, already-published
    definition of "security relevant" behind `result.owasp_relevant` — rather
    than inventing a second taxonomy. A finding whose `category` maps onto the
    OWASP Top 10 is a vulnerability; everything else is a general issue.
    """
    vulnerabilities = [f for f in findings if f.category in OWASP_RELEVANT_AREAS]
    issues = [f for f in findings if f.category not in OWASP_RELEVANT_AREAS]
    return vulnerabilities, issues


def _is_partial(result: ReviewResult) -> bool:
    """Whether the multi-agent pipeline ran but some specialist(s) did not complete.

    Only meaningful when `ai_analysis_ran` is True — a deterministic-only
    review never had specialists to fail in the first place.
    """
    return result.ai_analysis_ran and bool(result.failed_specialists)


def render_github_comment(result: ReviewResult, pr_slug: str) -> str:
    """Render the review comment posted on the GitHub pull request.

    Begins with the hidden marker so the comment can be found and updated on a
    later push instead of accumulating one comment per review.
    """
    emoji = _RISK_EMOJI.get(result.risk, "")
    human = "**REQUIRED**" if result.needs_human_review else "Not required"
    if _is_partial(result):
        heading = "## AIDA-MATE AI Review — PARTIAL"
    elif result.ai_analysis_ran:
        heading = "## AIDA-MATE AI Review"
    else:
        heading = "## AIDA-MATE Review"

    lines = [
        GITHUB_COMMENT_MARKER,
        heading,
        "",
        "| | |",
        "|---|---|",
        f"| **Risk** | {emoji} **{result.risk.value}** (score {result.risk_score}) |",
        f"| **Human review** | {human} |",
        f"| **Areas** | {_areas_line(result)} |",
        f"| **Security impact** | {'Yes' if result.security_impact else 'No'} |",
        f"| **OWASP relevant** | {'Yes' if result.owasp_relevant else 'No'} |",
        f"| **AI-assisted analysis** | {'Yes' if result.ai_analysis_ran else _AI_NOT_RUN} |",
    ]
    if result.ai_analysis_ran:
        # Checkable, not asserted: a reader can look at tool_calls_count and
        # judge for themselves whether the model actually investigated,
        # rather than trusting the "AI-assisted" row above on its own.
        lines.append(f"| **Model** | `{result.ai_model}` |")
        lines.append(f"| **Tool calls made** | {result.tool_calls_count} |")
        lines.append(f"| **Sandbox** | `{result.sandbox_mode}` |")
        if result.analysis_reused:
            lines.append("| **Analysis** | Reused from a previous review of this exact revision |")
    lines.append("")
    if _is_partial(result):
        failed = ", ".join(result.failed_specialists)
        lines.append(
            f"_⚠️ Partial analysis: the **{failed}** specialist(s) did not complete. Human review "
            "has been required to cover the gap — treat findings below as incomplete, not clean._"
        )
        lines.append("")
    if result.sandbox_mode == "local":
        lines.append(
            "_Sandbox mode `local` inspects the repository on the host filesystem — it is not "
            "Docker container isolation._"
        )
        lines.append("")

    if result.breakdown:
        contributions = " + ".join(
            f"{c.area.value} {c.points}" for c in result.breakdown
        )
        lines += [
            "### How this score was reached",
            "",
            f"`{contributions}` = **{result.risk_score}**",
            "",
            "_Risk is computed by a deterministic rule engine, not by the language model._",
            "",
        ]

    vulnerabilities, issues = _split_findings(result.findings)
    if vulnerabilities:
        lines += ["### Vulnerabilities", ""]
        lines += _github_finding_lines(vulnerabilities)
        lines.append("")
    if issues:
        lines += ["### Issues", ""]
        lines += _github_finding_lines(issues)
        lines.append("")

    lines += ["### Recommendation", "", _recommendation(result), ""]
    lines.append(f"<sub>AIDA-MATE reviewed `{pr_slug}`. Labels applied automatically.</sub>")

    return _truncate("\n".join(lines))


def _github_finding_lines(findings: list[Finding]) -> list[str]:
    """Render one section's worth of findings for the GitHub comment, capped."""
    lines = []
    for finding in findings[:MAX_FINDINGS_RENDERED]:
        location = f" — `{finding.file}`" if finding.file else ""
        location += f":{finding.line}" if finding.file and finding.line else ""
        lines.append(f"- **[{finding.severity.value}]** {finding.description}{location}")
        if finding.recommendation:
            lines.append(f"  - _Suggestion:_ {finding.recommendation}")
    remaining = len(findings) - MAX_FINDINGS_RENDERED
    if remaining > 0:
        lines.append(f"- _…and {remaining} more finding(s)._")
    return lines


def _linear_finding_lines(findings: list[Finding]) -> list[str]:
    """Render one section's worth of findings for the Linear comment, capped."""
    lines = []
    for finding in findings[:MAX_FINDINGS_RENDERED]:
        location = f" ({finding.file}:{finding.line})" if finding.file and finding.line else (
            f" ({finding.file})" if finding.file else ""
        )
        lines.append(f"- [{finding.severity.value}] {finding.description}{location}")
    remaining = len(findings) - MAX_FINDINGS_RENDERED
    if remaining > 0:
        lines.append(f"- …and {remaining} more finding(s).")
    return lines


def render_linear_comment(result: ReviewResult, pr_url: str, pr_slug: str) -> str:
    """Render the status update posted on the Linear issue.

    Shorter than the GitHub comment: whoever requested the review wants the
    verdict and a way back to the PR, not the full analysis.
    """
    emoji = _RISK_EMOJI.get(result.risk, "")
    human = "REQUIRED" if result.needs_human_review else "Not required"
    if _is_partial(result):
        headline = (
            f"**PARTIAL** AI analysis for [{pr_slug}]({pr_url}) — "
            f"the {', '.join(result.failed_specialists)} specialist(s) did not complete."
        )
    elif result.ai_analysis_ran:
        headline = f"AI analysis complete for [{pr_slug}]({pr_url})."
    else:
        headline = (
            f"Deterministic risk analysis complete for [{pr_slug}]({pr_url}) "
            "(the AI-assisted stage did not run for this review)."
        )

    lines = [
        "**AIDA-MATE Review**",
        "",
        headline,
        "",
        f"- **Risk:** {emoji} {result.risk.value} (score {result.risk_score})",
        f"- **Human review:** {human}",
        f"- **Areas:** {_areas_line(result)}",
        f"- **Security impact:** {'Yes' if result.security_impact else 'No'}",
    ]

    if result.analysis_reused:
        lines.append("- **Analysis:** reused from a previous review of this exact revision")

    if result.summary:
        lines += ["", result.summary]

    vulnerabilities, issues = _split_findings(result.findings)
    if vulnerabilities:
        lines += ["", "**Vulnerabilities:**"]
        lines += _linear_finding_lines(vulnerabilities)
    if issues:
        lines += ["", "**Issues:**"]
        lines += _linear_finding_lines(issues)

    return _truncate("\n".join(lines), limit=10_000)


def _recommendation(result: ReviewResult) -> str:
    """One-line verdict tailored to the risk level."""
    if result.needs_human_review:
        return (
            "Human review is required before merging this pull request. "
            "The changes touch areas where a mistake is costly to reverse."
        )
    if result.risk is RiskLevel.MEDIUM:
        return "Merging is reasonable after the usual review. Nothing high-risk was detected."
    return "Low risk. No special review beyond your normal process is needed."
