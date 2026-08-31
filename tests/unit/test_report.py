"""Rendering of the GitHub and Linear review comments."""

from app.core.report import (
    GITHUB_COMMENT_MARKER,
    MAX_FINDINGS_RENDERED,
    render_github_comment,
    render_linear_comment,
)
from app.models.common import Area, RiskLevel, Severity
from app.models.review import Finding, ReviewResult, ScoreContribution


def _result(**overrides) -> ReviewResult:
    values = {
        "risk": RiskLevel.HIGH,
        "risk_score": 80,
        "needs_human_review": True,
        "labels": ["risk: high", "area: auth"],
        "areas": [Area.AUTHENTICATION, Area.API],
        "security_impact": True,
        "owasp_relevant": True,
        "summary": "Authentication and API logic were modified.",
        "findings": [],
        "breakdown": [
            ScoreContribution(area=Area.AUTHENTICATION, points=50, rule="authentication touched"),
            ScoreContribution(area=Area.API, points=30, rule="api touched"),
        ],
    }
    values.update(overrides)
    return ReviewResult(**values)


# --- GitHub comment ----------------------------------------------------------


def test_github_comment_starts_with_the_marker() -> None:
    """The marker is how the comment is found and updated on re-review."""
    assert render_github_comment(_result(), "acme/api#431").startswith(GITHUB_COMMENT_MARKER)


def test_github_comment_states_the_verdict() -> None:
    body = render_github_comment(_result(), "acme/api#431")

    assert "AIDA-MATE AI Review" in body
    assert "HIGH" in body
    assert "score 80" in body
    assert "REQUIRED" in body


def test_github_comment_lists_areas_and_flags() -> None:
    body = render_github_comment(_result(), "acme/api#431")

    assert "authentication" in body
    assert "api" in body
    assert "OWASP relevant" in body


def test_github_comment_shows_the_score_breakdown() -> None:
    """A HIGH verdict must be justifiable, not asserted."""
    body = render_github_comment(_result(), "acme/api#431")

    assert "authentication 50" in body
    assert "api 30" in body
    assert "= **80**" in body


def test_github_comment_states_that_risk_is_deterministic() -> None:
    """Readers should know the number is not a model's opinion."""
    body = render_github_comment(_result(), "acme/api#431")

    assert "deterministic" in body.lower()
    assert "not by the language model" in body.lower()


def test_github_comment_renders_findings() -> None:
    finding = Finding(
        category=Area.SECURITY,
        severity=Severity.HIGH,
        description="Password compared with ==",
        file="app/auth/login.py",
        line=42,
        recommendation="Use a constant-time comparison.",
    )
    body = render_github_comment(_result(findings=[finding]), "acme/api#431")

    assert "Password compared with ==" in body
    assert "app/auth/login.py" in body
    assert "42" in body
    assert "constant-time" in body
    assert "### Vulnerabilities" in body


def test_github_comment_splits_vulnerabilities_from_issues() -> None:
    """A security-relevant category (OWASP_RELEVANT_AREAS) is a vulnerability;
    everything else is a general issue — two distinct sections, not one list."""
    vuln = Finding(category=Area.AUTHENTICATION, severity=Severity.HIGH, description="Weak token check")
    issue = Finding(category=Area.TESTING, severity=Severity.LOW, description="Missing test coverage")
    body = render_github_comment(_result(findings=[vuln, issue]), "acme/api#431")

    assert "### Vulnerabilities" in body
    assert "### Issues" in body
    assert body.index("### Vulnerabilities") < body.index("Weak token check")
    assert body.index("### Issues") < body.index("Missing test coverage")
    assert "Weak token check" not in body.split("### Issues")[1]


def test_github_comment_omits_a_section_with_no_matching_findings() -> None:
    finding = Finding(category=Area.TESTING, severity=Severity.LOW, description="Missing test coverage")
    body = render_github_comment(_result(findings=[finding]), "acme/api#431")

    assert "### Vulnerabilities" not in body
    assert "### Issues" in body


def test_github_comment_caps_the_findings_list() -> None:
    findings = [
        Finding(category=Area.BACKEND, severity=Severity.LOW, description=f"issue {i}")
        for i in range(MAX_FINDINGS_RENDERED + 10)
    ]
    body = render_github_comment(_result(findings=findings), "acme/api#431")

    assert "10 more finding" in body


def test_low_risk_comment_does_not_demand_review() -> None:
    body = render_github_comment(
        _result(risk=RiskLevel.LOW, risk_score=1, needs_human_review=False), "acme/api#431"
    )

    assert "Low risk" in body
    assert "Not required" in body


def test_comment_is_bounded() -> None:
    """GitHub rejects bodies over 65536 characters."""
    findings = [
        Finding(category=Area.BACKEND, severity=Severity.LOW, description="x" * 5000)
        for _ in range(100)
    ]
    body = render_github_comment(_result(findings=findings), "acme/api#431")

    assert len(body) <= 60_000


# --- Linear comment ----------------------------------------------------------


def test_linear_comment_links_back_to_the_pr() -> None:
    body = render_linear_comment(_result(), "https://github.com/acme/api/pull/431", "acme/api#431")

    assert "https://github.com/acme/api/pull/431" in body
    assert "acme/api#431" in body


def test_linear_comment_carries_the_verdict() -> None:
    body = render_linear_comment(_result(), "https://github.com/acme/api/pull/431", "acme/api#431")

    assert "AIDA-MATE Review" in body
    assert "HIGH" in body
    assert "REQUIRED" in body
    assert "authentication" in body


def test_linear_comment_is_shorter_than_the_github_one() -> None:
    """Different audiences: the requester wants the verdict, not the analysis."""
    result = _result(
        findings=[
            Finding(category=Area.BACKEND, severity=Severity.LOW, description=f"issue {i}")
            for i in range(20)
        ]
    )

    github = render_github_comment(result, "acme/api#431")
    linear = render_linear_comment(result, "https://github.com/acme/api/pull/431", "acme/api#431")

    assert len(linear) < len(github)


def test_linear_comment_lists_findings_under_the_pr() -> None:
    """The PR link/verdict sits at the top; findings render as points below it."""
    finding = Finding(
        category=Area.SECURITY,
        severity=Severity.HIGH,
        description="Password compared with ==",
        file="app/auth/login.py",
        line=42,
    )
    body = render_linear_comment(
        _result(findings=[finding]), "https://github.com/acme/api/pull/431", "acme/api#431"
    )

    assert "Vulnerabilities:" in body
    assert body.index("acme/api#431") < body.index("Vulnerabilities:")
    assert "Password compared with ==" in body
    assert "app/auth/login.py:42" in body


def test_linear_comment_splits_vulnerabilities_from_issues() -> None:
    vuln = Finding(category=Area.AUTHENTICATION, severity=Severity.HIGH, description="Weak token check")
    issue = Finding(category=Area.TESTING, severity=Severity.LOW, description="Missing test coverage")
    body = render_linear_comment(
        _result(findings=[vuln, issue]), "https://github.com/acme/api/pull/431", "acme/api#431"
    )

    assert "**Vulnerabilities:**" in body
    assert "**Issues:**" in body
    assert "Weak token check" not in body.split("**Issues:**")[1]


def test_linear_comment_caps_the_findings_list() -> None:
    findings = [
        Finding(category=Area.BACKEND, severity=Severity.LOW, description=f"issue {i}")
        for i in range(MAX_FINDINGS_RENDERED + 10)
    ]
    body = render_linear_comment(
        _result(findings=findings), "https://github.com/acme/api/pull/431", "acme/api#431"
    )

    assert "10 more finding" in body


def test_linear_comment_omits_findings_section_when_there_are_none() -> None:
    body = render_linear_comment(_result(findings=[]), "https://github.com/acme/api/pull/431", "acme/api#431")

    assert "Vulnerabilities:" not in body
    assert "Issues:" not in body


def test_linear_comment_has_no_hidden_marker() -> None:
    """Linear comments are not updated in place, so no marker is needed."""
    body = render_linear_comment(_result(), "https://x", "acme/api#431")

    assert GITHUB_COMMENT_MARKER not in body


def test_empty_areas_render_explicitly() -> None:
    body = render_github_comment(_result(areas=[]), "acme/api#431")

    assert "none detected" in body


# --- AI-analysis honesty ------------------------------------------------------
#
# The agent/sandbox stage is optional; published text must say so rather than
# unconditionally claiming an AI reviewed the PR when only the deterministic
# path ran.


def test_github_comment_claims_ai_review_when_it_ran() -> None:
    body = render_github_comment(_result(ai_analysis_ran=True), "acme/api#431")

    assert "AIDA-MATE AI Review" in body
    assert "AI-assisted analysis** | Yes" in body


def test_github_comment_is_honest_when_ai_did_not_run() -> None:
    body = render_github_comment(_result(ai_analysis_ran=False), "acme/api#431")

    assert "## AIDA-MATE AI Review" not in body
    assert "## AIDA-MATE Review" in body
    assert "deterministic only" in body.lower()


def test_linear_comment_claims_ai_analysis_when_it_ran() -> None:
    body = render_linear_comment(
        _result(ai_analysis_ran=True), "https://github.com/acme/api/pull/431", "acme/api#431"
    )

    assert "AI analysis complete" in body


def test_linear_comment_is_honest_when_ai_did_not_run() -> None:
    body = render_linear_comment(
        _result(ai_analysis_ran=False), "https://github.com/acme/api/pull/431", "acme/api#431"
    )

    assert "AI analysis complete" not in body
    assert "Deterministic risk analysis complete" in body
    assert "did not run" in body


# --- Agent run metadata (model, tool calls, sandbox mode) ---------------------
#
# Checkable, not asserted: a reader must be able to look at how many tool
# calls actually happened rather than trust the "AI-assisted" row alone.


def test_github_comment_shows_model_and_tool_call_count_when_ai_ran() -> None:
    body = render_github_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", tool_calls_count=7, sandbox_mode="local"),
        "acme/api#431",
    )

    assert "gpt-5.5" in body
    assert "Tool calls made** | 7" in body


def test_github_comment_omits_agent_metadata_when_ai_did_not_run() -> None:
    """None of these fields mean anything when the agent never executed."""
    body = render_github_comment(_result(ai_analysis_ran=False), "acme/api#431")

    assert "Model**" not in body
    assert "Tool calls made**" not in body
    assert "Sandbox**" not in body


def test_github_comment_shows_zero_tool_calls_honestly() -> None:
    """Zero is a legitimate result for a trivial PR, not hidden or misrepresented."""
    body = render_github_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", tool_calls_count=0), "acme/api#431"
    )

    assert "Tool calls made** | 0" in body


def test_github_comment_flags_local_sandbox_as_not_isolated() -> None:
    body = render_github_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", sandbox_mode="local"), "acme/api#431"
    )

    assert "not Docker container isolation" in body


def test_github_comment_does_not_flag_docker_sandbox() -> None:
    body = render_github_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", sandbox_mode="docker"), "acme/api#431"
    )

    assert "not Docker container isolation" not in body


# --- Partial multi-agent runs (some specialist(s) failed) --------------------
#
# `failed_specialists` is only meaningful when `ai_analysis_ran` is True — a
# deterministic-only review never had specialists that could fail.


def test_github_comment_heading_is_partial_when_specialists_failed() -> None:
    body = render_github_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", failed_specialists=["security"]),
        "acme/api#431",
    )

    assert "## AIDA-MATE AI Review — PARTIAL" in body
    assert "## AIDA-MATE AI Review\n" not in body


def test_github_comment_names_the_failed_specialists() -> None:
    body = render_github_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", failed_specialists=["security", "testing"]),
        "acme/api#431",
    )

    assert "security, testing" in body
    assert "did not complete" in body.lower()


def test_github_comment_heading_is_not_partial_without_failures() -> None:
    body = render_github_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", failed_specialists=[]), "acme/api#431"
    )

    assert "PARTIAL" not in body


def test_github_comment_is_not_partial_when_ai_did_not_run() -> None:
    """failed_specialists is meaningless without a multi-agent run to fail."""
    body = render_github_comment(_result(ai_analysis_ran=False, failed_specialists=[]), "acme/api#431")

    assert "PARTIAL" not in body


def test_linear_comment_headline_is_partial_when_specialists_failed() -> None:
    body = render_linear_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", failed_specialists=["architecture"]),
        "https://github.com/acme/api/pull/431",
        "acme/api#431",
    )

    assert "**PARTIAL**" in body
    assert "architecture" in body


def test_linear_comment_headline_is_not_partial_without_failures() -> None:
    body = render_linear_comment(
        _result(ai_analysis_ran=True, ai_model="gpt-5.5", failed_specialists=[]),
        "https://github.com/acme/api/pull/431",
        "acme/api#431",
    )

    assert "PARTIAL" not in body
    assert "AI analysis complete" in body
