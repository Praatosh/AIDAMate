"""Domain model behaviour and invariants."""

import pytest
from pydantic import ValidationError

from app.models.common import Area, MergeStatus, ReviewJobStatus, RiskLevel, Severity
from app.models.github import ChangedFile, FileChangeStatus, PullRequestRef, RepositoryRef
from app.models.review import (
    Finding,
    PRContextAnalysis,
    ReviewAnalysis,
    ReviewJob,
    ReviewResult,
    build_content_key,
)

# --- The central invariant --------------------------------------------------


def test_agent_output_cannot_carry_a_risk_verdict() -> None:
    """`ReviewAnalysis` must have no risk/label/human-review fields.

    This is the schema-level guarantee behind "the LLM does not decide risk":
    the model is not merely instructed to abstain, it has nowhere to put a
    verdict. If this test fails, that guarantee has been weakened.
    """
    forbidden = {
        "risk",
        "risk_score",
        "risk_level",
        "labels",
        "needs_human_review",
        "suggested_risk",
    }

    assert forbidden.isdisjoint(ReviewAnalysis.model_fields)


def test_context_agent_output_cannot_carry_a_risk_verdict_either() -> None:
    """Same schema-level guarantee for the Context Agent's output — it is
    orientation for the specialists, not a risk judgment of its own."""
    forbidden = {
        "risk",
        "risk_score",
        "risk_level",
        "labels",
        "needs_human_review",
        "suggested_risk",
    }

    assert forbidden.isdisjoint(PRContextAnalysis.model_fields)


def test_agent_supplied_risk_field_is_discarded() -> None:
    """Even if a model emits a risk field, it must not survive parsing."""
    analysis = ReviewAnalysis.model_validate(
        {"summary": "s", "risk": "LOW", "needs_human_review": False}
    )

    assert not hasattr(analysis, "risk")
    assert not hasattr(analysis, "needs_human_review")


def test_published_result_does_carry_the_verdict() -> None:
    """`ReviewResult` — assembled by the engines — is where the verdict lives."""
    for field in ("risk", "risk_score", "needs_human_review", "labels"):
        assert field in ReviewResult.model_fields


def test_failed_specialists_defaults_to_empty_on_agent_run_outcome() -> None:
    from app.models.review import AgentRunOutcome

    outcome = AgentRunOutcome(
        analysis=ReviewAnalysis(summary="s"), model="gpt-5.6-sol", tool_calls_count=0
    )

    assert outcome.failed_specialists == []


def test_failed_specialists_defaults_to_empty_on_review_result() -> None:
    result = ReviewResult(
        risk=RiskLevel.LOW, risk_score=0, needs_human_review=False, labels=[]
    )

    assert result.failed_specialists == []


# --- Taxonomy ---------------------------------------------------------------


def test_area_values_are_label_safe() -> None:
    """Area values map directly onto `area:*` labels, so no spaces or capitals."""
    for area in Area:
        assert area.value == area.value.lower()
        assert " " not in area.value


def test_risk_and_severity_are_distinct_scales() -> None:
    """A PR-level risk level is not a finding-level severity."""
    assert {level.value for level in RiskLevel} == {"LOW", "MEDIUM", "HIGH"}
    assert len({s.value for s in Severity}) == 5
    assert {s.value for s in Severity}.isdisjoint({level.value for level in RiskLevel})


# --- Job lifecycle ----------------------------------------------------------


def test_terminal_states() -> None:
    assert ReviewJobStatus.COMPLETED.is_terminal
    assert ReviewJobStatus.FAILED.is_terminal
    assert not ReviewJobStatus.QUEUED.is_terminal
    assert not ReviewJobStatus.ANALYZING.is_terminal


def _job() -> ReviewJob:
    return ReviewJob(idempotency_key="k", linear_issue_id="issue-1")


def test_job_starts_queued() -> None:
    job = _job()

    assert job.status is ReviewJobStatus.QUEUED
    assert job.started_at is None
    assert job.completed_at is None
    assert job.duration_ms is None


def test_provisioning_records_start_time() -> None:
    job = _job()
    job.mark_status(ReviewJobStatus.PROVISIONING)

    assert job.started_at is not None


def test_completion_records_duration() -> None:
    job = _job()
    job.mark_status(ReviewJobStatus.PROVISIONING)
    job.mark_status(ReviewJobStatus.COMPLETED)

    assert job.completed_at is not None
    assert job.duration_ms is not None
    assert job.duration_ms >= 0


def test_mark_failed_records_code_and_message() -> None:
    job = _job()
    job.mark_failed("sandbox_unavailable", "No sandbox configured.")

    assert job.status is ReviewJobStatus.FAILED
    assert job.error_code == "sandbox_unavailable"
    assert job.completed_at is not None


def test_log_context_has_the_required_observability_fields() -> None:
    job = _job()

    assert set(job.log_context()) >= {"review_id", "linear_issue_id", "github_pr", "status"}


# --- Gated auto-merge (CLAUDE.md §1a) ----------------------------------------


def test_job_starts_with_no_merge_status() -> None:
    assert _job().merge_status is None


def test_mark_merge_pending_records_status_and_timestamp() -> None:
    job = _job()
    job.mark_merge_pending()

    assert job.merge_status is MergeStatus.PENDING_CONFIRMATION
    assert job.merge_requested_at is not None


def test_mark_merged_does_not_touch_review_status() -> None:
    """Merge outcome is orthogonal to the review lifecycle — COMPLETED stays settled."""
    job = _job()
    job.mark_status(ReviewJobStatus.COMPLETED)
    job.mark_merged()

    assert job.merge_status is MergeStatus.MERGED
    assert job.status is ReviewJobStatus.COMPLETED


def test_mark_merge_declined() -> None:
    job = _job()
    job.mark_merge_declined()

    assert job.merge_status is MergeStatus.DECLINED
    assert job.merge_decided_at is not None


def test_mark_merge_failed_records_message() -> None:
    job = _job()
    job.mark_merge_failed("not mergeable")

    assert job.merge_status is MergeStatus.FAILED
    assert job.merge_error == "not mergeable"


# --- Idempotency ------------------------------------------------------------


def test_content_key_is_stable() -> None:
    assert build_content_key("issue-1", 42, "abc123") == build_content_key("issue-1", 42, "abc123")


def test_new_commits_produce_a_new_key() -> None:
    """A genuinely changed PR should be re-reviewed."""
    assert build_content_key("issue-1", 42, "abc") != build_content_key("issue-1", 42, "def")


def test_different_prs_produce_different_keys() -> None:
    assert build_content_key("issue-1", 1, "abc") != build_content_key("issue-1", 2, "abc")


def test_missing_sha_still_yields_a_usable_key() -> None:
    assert build_content_key("issue-1", 42, None) == "issue-1:42:unknown"


# --- GitHub models ----------------------------------------------------------


def test_repository_full_name() -> None:
    assert RepositoryRef(owner="acme", name="api").full_name == "acme/api"


def test_pull_request_slug() -> None:
    ref = PullRequestRef(
        repository=RepositoryRef(owner="acme", name="api"),
        number=42,
        url="https://github.com/acme/api/pull/42",
    )

    assert ref.slug == "acme/api#42"


def test_pr_number_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        PullRequestRef(
            repository=RepositoryRef(owner="a", name="b"), number=0, url="https://example.com"
        )


def test_changed_file_defaults() -> None:
    changed = ChangedFile(filename="app/main.py", status=FileChangeStatus.MODIFIED)

    assert (changed.additions, changed.deletions, changed.patch) == (0, 0, None)


# --- Findings ---------------------------------------------------------------


def test_finding_requires_a_known_category() -> None:
    with pytest.raises(ValidationError):
        Finding(category="not-a-real-area", severity=Severity.HIGH, description="x")


def test_finding_line_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Finding(
            category=Area.SECURITY, severity=Severity.HIGH, description="x", file="a.py", line=0
        )
