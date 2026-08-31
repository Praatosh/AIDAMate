"""End-to-end pipeline: resolve, fetch, classify, publish.

The full deterministic slice runs here with recording fakes, so what AIDA-MATE
would write to GitHub and Linear is asserted without a network.
"""

import pytest

from app.agents.orchestrator import ReviewOrchestrator
from app.core.errors import LinearError, LinkedPullRequestNotFoundError
from app.core.risk_engine import RiskThresholds
from app.models.common import Area, ReviewJobStatus, RiskLevel, Severity
from app.models.github import ChangedFile, FileChangeStatus, PullRequest, PullRequestRef, RepositoryRef
from app.models.linear import AgentActivityType, LinearAttachment, LinearIssue
from app.models.review import AgentRunOutcome, Finding, ReviewAnalysis, ReviewJob, build_content_key
from app.services.job_repository import InMemoryReviewJobRepository
from app.services.pr_resolver import build_resolver

REPO = RepositoryRef(owner="acme", name="api")
REF = PullRequestRef(repository=REPO, number=431, url="https://github.com/acme/api/pull/431")

#: A PR touching auth + API + a migration — the flow-diagram scenario.
HIGH_RISK_FILES = [
    "app/auth/middleware.py",
    "app/api/users.py",
    "migrations/0004_oauth.sql",
]


class FakeLinear:
    """Records what would be written back to Linear."""

    def __init__(self, issue: LinearIssue, fail: bool = False) -> None:
        self._issue = issue
        self._fail = fail
        self.comments: list[tuple[str, str]] = []
        self.activities: list[tuple[str, AgentActivityType, str]] = []

    async def get_issue(self, issue_id: str, *, organization_id=None) -> LinearIssue:
        return self._issue

    async def add_comment(self, issue_id: str, body: str, *, organization_id=None) -> None:
        if self._fail:
            raise LinearError("Linear is down")
        self.comments.append((issue_id, body))

    async def emit_agent_activity(self, session_id, activity_type, body, *, organization_id=None):
        if self._fail:
            raise LinearError("Linear is down")
        self.activities.append((session_id, activity_type, body))


class FakeGitHub:
    """Records what would be written to GitHub."""

    def __init__(self, pull: PullRequest | None = None) -> None:
        self._pull = pull
        self.fetched: list[str] = []
        self.ensured: dict[str, str] = {}
        self.applied: set[str] = set()
        self.exclusive_prefixes: tuple[str, ...] = ()
        self.comments: list[str] = []

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequest:
        self.fetched.append(ref.slug)
        return self._pull or _pull_request()

    async def list_open_pull_requests(self, repository, *, limit: int = 100):
        return []

    async def search_pull_requests(self, repository, text: str):
        return []

    async def ensure_labels_exist(self, repo, labels: dict[str, str]) -> None:
        self.ensured.update(labels)

    async def apply_labels(self, ref, labels: set[str], *, exclusive_prefixes=()) -> None:
        self.applied = set(labels)
        self.exclusive_prefixes = exclusive_prefixes

    async def add_comment(self, ref, body: str, *, marker=None) -> int:
        self.comments.append(body)
        return 1

    async def download_archive(self, repo, sha: str) -> bytes:
        self.downloaded = (repo, sha)
        return b"fake-archive-bytes"


class FakeSandbox:
    """Records calls; scripts exec() results; never touches a real subprocess."""

    id = "fake-sandbox-id"

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, bytes]] = []
        self.exec_calls: list[str] = []
        self.destroyed = False
        self._exec_exit_code = 0

    def script_extract_failure(self) -> None:
        self._exec_exit_code = 1

    async def upload_bytes(self, dest_path: str, content: bytes) -> None:
        self.uploaded.append((dest_path, content))

    async def exec(self, command: str, *, cwd=None, timeout_s=None):
        self.exec_calls.append(command)
        return _FakeExecResult(self._exec_exit_code)

    async def read_file(self, path: str, *, max_bytes: int = 200_000) -> str:
        return ""

    async def destroy(self) -> None:
        self.destroyed = True


class _FakeExecResult:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.stdout = ""
        self.stderr = "extraction failed" if exit_code else ""


class FakeSandboxFactory:
    def __init__(self, sandbox: FakeSandbox, *, fail: bool = False) -> None:
        self._sandbox = sandbox
        self._fail = fail

    async def create(self, *, labels=None):
        if self._fail:
            from app.core.errors import SandboxUnavailableError

            raise SandboxUnavailableError("docker not available")
        return self._sandbox


class FakeAgentRunner:
    """Returns a scripted `AgentRunOutcome`, or raises, without any real LLM call."""

    def __init__(
        self,
        analysis=None,
        *,
        raise_error: Exception | None = None,
        model: str = "fake-model",
        tool_calls_count: int = 0,
        failed_specialists: list[str] | None = None,
    ) -> None:
        self._analysis = analysis
        self._raise_error = raise_error
        self._model = model
        self._tool_calls_count = tool_calls_count
        self._failed_specialists = failed_specialists or []
        self.analyze_calls: list[tuple] = []

    async def analyze(self, pull_request: PullRequest, sandbox, *, review_id: str) -> AgentRunOutcome:
        self.analyze_calls.append((pull_request, sandbox, review_id))
        if self._raise_error:
            raise self._raise_error
        analysis = self._analysis or ReviewAnalysis(summary="No findings.", findings=[])
        return AgentRunOutcome(
            analysis=analysis,
            model=self._model,
            tool_calls_count=self._tool_calls_count,
            failed_specialists=self._failed_specialists,
        )


def _pull_request(head_sha: str = "3f2c9ab", files: list[str] | None = None) -> PullRequest:
    return PullRequest(
        ref=REF,
        title="Add OAuth authentication",
        base_ref="main",
        head_ref="feature/mate-123-oauth",
        head_sha=head_sha,
        changed_files=[
            ChangedFile(filename=name, status=FileChangeStatus.MODIFIED)
            for name in (files or HIGH_RISK_FILES)
        ],
    )


def _issue(with_pr: bool = True) -> LinearIssue:
    return LinearIssue(
        id="issue-1",
        identifier="MATE-123",
        title="Add OAuth authentication",
        attachments=[LinearAttachment(url="https://github.com/acme/api/pull/431")] if with_pr else [],
    )


def _job(session: str | None = None) -> ReviewJob:
    return ReviewJob(
        idempotency_key="session:s-1",
        linear_issue_id="issue-1",
        agent_session_id=session,
        organization_id="org-1",
    )


def _build(repo, *, with_pr=True, pull=None, linear_fails=False):
    github = FakeGitHub(pull)
    linear = FakeLinear(_issue(with_pr), fail=linear_fails)
    orchestrator = ReviewOrchestrator(
        linear=linear,
        resolver=build_resolver(github, [REPO]),
        github=github,
        repository=repo,
        thresholds=RiskThresholds(),
    )
    return orchestrator, github, linear


def _build_with_agent(repo, sandbox_factory, agent_runner, *, pull=None):
    """Like `_build`, but with the optional sandbox/agent stage wired in."""
    github = FakeGitHub(pull)
    linear = FakeLinear(_issue())
    orchestrator = ReviewOrchestrator(
        linear=linear,
        resolver=build_resolver(github, [REPO]),
        github=github,
        repository=repo,
        thresholds=RiskThresholds(),
        sandbox_factory=sandbox_factory,
        agent_runner=agent_runner,
        agent_timeout_s=5.0,
    )
    return orchestrator, github, linear


@pytest.fixture
def repo() -> InMemoryReviewJobRepository:
    return InMemoryReviewJobRepository()


# --- The full slice ----------------------------------------------------------


async def test_pipeline_runs_to_completion(repo) -> None:
    """Resolve, fetch, classify, and publish — with no sandbox and no model."""
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.pull_request.slug == "acme/api#431"
    assert job.head_sha == "3f2c9ab"
    assert job.result is not None
    assert github.comments


async def test_high_risk_pr_is_classified_and_labelled(repo) -> None:
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.risk_level is RiskLevel.HIGH
    assert "risk: high" in github.applied
    assert "review: needs-human" in github.applied
    assert "review: ai-reviewed" in github.applied
    assert "area: auth" in github.applied
    assert "owasp: relevant" in github.applied


async def test_low_risk_pr_is_not_escalated(repo) -> None:
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo, pull=_pull_request(files=["README.md"]))

    await orchestrator.execute(job)

    assert job.risk_level is RiskLevel.LOW
    assert "risk: low" in github.applied
    assert "review: needs-human" not in github.applied


async def test_labels_are_created_before_being_applied(repo) -> None:
    """Applying a label that does not exist would fail on a fresh repository."""
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo)

    await orchestrator.execute(job)

    assert github.applied <= set(github.ensured)
    assert all(len(colour) == 6 for colour in github.ensured.values())


async def test_risk_namespace_is_marked_exclusive(repo) -> None:
    """Otherwise a re-review leaves `risk: low` next to `risk: high`."""
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo)

    await orchestrator.execute(job)

    assert "risk" in github.exclusive_prefixes


async def test_github_comment_contains_the_verdict(repo) -> None:
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo)

    await orchestrator.execute(job)

    body = github.comments[0]
    # No sandbox/agent wired via `_build`, so this must not claim an AI reviewed it.
    assert "## AIDA-MATE Review" in body
    assert "## AIDA-MATE AI Review" not in body
    assert "HIGH" in body
    assert "authentication 50" in body


# --- Linear publication ------------------------------------------------------


async def test_assignment_trigger_posts_a_linear_comment(repo) -> None:
    job = await repo.create(_job(session=None))
    orchestrator, _, linear = _build(repo)

    await orchestrator.execute(job)

    assert len(linear.comments) == 1
    assert "AIDA-MATE Review" in linear.comments[0][1]


async def test_agent_session_receives_a_response_activity(repo) -> None:
    """A session is the richer surface; the result belongs in it, not beside it."""
    job = await repo.create(_job(session="session-1"))
    orchestrator, _, linear = _build(repo)

    await orchestrator.execute(job)

    assert linear.comments == []
    assert linear.activities[-1][1] is AgentActivityType.RESPONSE
    assert "HIGH" in linear.activities[-1][2]


async def test_linear_failure_does_not_fail_the_review(repo) -> None:
    """The developer already has the result on the PR, where the work happens."""
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo, linear_fails=True)

    await orchestrator.execute(job)

    assert github.comments
    assert job.result is not None


# --- Ordering and durability -------------------------------------------------


async def test_result_is_persisted_before_publication(repo) -> None:
    """A GitHub outage must not destroy a completed analysis."""
    job = await repo.create(_job())

    class ExplodingGitHub(FakeGitHub):
        async def ensure_labels_exist(self, repo, labels):
            raise RuntimeError("GitHub is down")

    github = ExplodingGitHub()
    orchestrator = ReviewOrchestrator(
        linear=FakeLinear(_issue()),
        resolver=build_resolver(github, [REPO]),
        github=github,
        repository=repo,
    )

    with pytest.raises(RuntimeError):
        await orchestrator.execute(job)

    stored = await repo.get(job.id)
    assert stored.result is not None
    assert stored.risk_level is RiskLevel.HIGH


async def test_missing_pr_fails_before_any_fetch_or_write(repo) -> None:
    """No linked PR costs a lookup — no fetch, no labels, no sandbox."""
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo, with_pr=False)

    with pytest.raises(LinkedPullRequestNotFoundError):
        await orchestrator.execute(job)

    assert github.fetched == []
    assert github.applied == set()
    assert github.comments == []


async def test_status_reaches_updating_linear(repo) -> None:
    job = await repo.create(_job())
    orchestrator, _, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.status is ReviewJobStatus.UPDATING_LINEAR


# --- Recorded detail ---------------------------------------------------------


async def test_issue_identifier_is_recorded(repo) -> None:
    job = await repo.create(_job())
    orchestrator, _, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.linear_issue_identifier == "MATE-123"


async def test_result_carries_areas_and_breakdown(repo) -> None:
    job = await repo.create(_job())
    orchestrator, _, _ = _build(repo)

    await orchestrator.execute(job)

    assert Area.AUTHENTICATION in job.result.areas
    assert sum(c.points for c in job.result.breakdown) == job.result.risk_score


# --- Content dedup -----------------------------------------------------------


async def test_content_key_is_recorded(repo) -> None:
    job = await repo.create(_job())
    orchestrator, _, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.content_key == build_content_key("issue-1", 431, "3f2c9ab")


async def test_new_commits_produce_a_new_content_key(repo) -> None:
    first = await repo.create(_job())
    await _build(repo, pull=_pull_request("sha-one"))[0].execute(first)

    second = await repo.create(ReviewJob(idempotency_key="session:s-2", linear_issue_id="issue-1"))
    await _build(repo, pull=_pull_request("sha-two"))[0].execute(second)

    assert first.content_key != second.content_key


async def test_completed_review_of_the_same_revision_is_found(repo) -> None:
    done = ReviewJob(
        idempotency_key="session:old",
        linear_issue_id="issue-1",
        content_key=build_content_key("issue-1", 431, "3f2c9ab"),
    )
    done.mark_status(ReviewJobStatus.COMPLETED)
    await repo.create(done)

    found = await repo.find_completed_by_content_key(done.content_key)

    assert found is not None and found.id == done.id


async def test_failed_review_does_not_count_as_reviewed(repo) -> None:
    failed = ReviewJob(
        idempotency_key="session:old",
        linear_issue_id="issue-1",
        content_key=build_content_key("issue-1", 431, "3f2c9ab"),
    )
    failed.mark_failed("boom", "failed")
    await repo.create(failed)

    assert await repo.find_completed_by_content_key(failed.content_key) is None


# --- Content dedup is ENFORCED, not merely observed ---------------------------
#
# This is what makes a Linear assignment mean "AIDA-MATE owns this issue" rather
# than "run exactly one review": re-delegating an unchanged PR must cost
# nothing beyond the PR fetch needed to learn the SHA.


async def _completed_review_of_the_default_revision(repo) -> ReviewJob:
    done = ReviewJob(
        idempotency_key="session:earlier",
        linear_issue_id="issue-1",
        content_key=build_content_key("issue-1", 431, "3f2c9ab"),
    )
    done.mark_status(ReviewJobStatus.COMPLETED)
    await repo.create(done)
    return done


async def test_rereviewing_the_same_revision_is_skipped(repo) -> None:
    previous = await _completed_review_of_the_default_revision(repo)
    job = await repo.create(_job())
    orchestrator, github, linear = _build(repo)

    await orchestrator.execute(job)

    assert job.status is ReviewJobStatus.SKIPPED
    assert previous.id in (job.error or "")
    # Nothing was published: the earlier result still stands, untouched.
    assert github.applied == set()
    assert github.comments == []
    assert linear.comments == []


async def test_explicit_assignment_rereviews_the_same_revision(repo) -> None:
    await _completed_review_of_the_default_revision(repo)
    job = _job()
    job.trigger_source = "issue_assignment"
    await repo.create(job)
    orchestrator, github, linear = _build(repo)

    await orchestrator.execute(job)

    assert job.status is ReviewJobStatus.UPDATING_LINEAR
    assert github.comments
    assert linear.comments


async def test_explicit_rereview_of_unchanged_revision_reuses_previous_findings(repo) -> None:
    """Re-delegating an unchanged revision must not change the score.

    The AI stage is not perfectly reproducible run-to-run, so a second full pass over the
    same diff could otherwise surface a different finding set — and therefore a different
    score — for the exact same PR revision. This is also the latency fix: no sandbox or
    agent call happens on the reused path.
    """
    first_runner = FakeAgentRunner(
        ReviewAnalysis(
            summary="Touches billing.",
            findings=[Finding(category=Area.PAYMENTS, severity=Severity.LOW, description="fee calc")],
        ),
        model="fake-model-v1",
        tool_calls_count=7,
    )
    first_orchestrator, _, _ = _build_with_agent(
        repo, FakeSandboxFactory(FakeSandbox()), first_runner, pull=_pull_request(files=["README.md"])
    )
    first_job = await repo.create(_job())
    await first_orchestrator.execute(first_job)
    first_job.mark_status(ReviewJobStatus.COMPLETED)  # the worker's job normally, not the orchestrator's
    assert first_job.risk_level is RiskLevel.MEDIUM  # documentation(1) + payments(40) = 41

    second_runner = FakeAgentRunner(raise_error=RuntimeError("must not run on a reused revision"))
    second_orchestrator, second_github, second_linear = _build_with_agent(
        repo, FakeSandboxFactory(FakeSandbox()), second_runner, pull=_pull_request(files=["README.md"])
    )
    second_job = _job()
    second_job.trigger_source = "issue_assignment"
    await repo.create(second_job)

    await second_orchestrator.execute(second_job)

    assert second_runner.analyze_calls == []
    assert second_job.risk_level == first_job.risk_level
    assert second_job.result.risk_score == first_job.result.risk_score
    assert second_job.result.analysis_reused is True
    assert second_job.result.ai_model == "fake-model-v1"
    assert second_github.comments
    assert second_linear.comments


async def test_skipped_review_records_no_error_code(repo) -> None:
    """A skip is a deliberate decision, not a failure to report as one."""
    await _completed_review_of_the_default_revision(repo)
    job = await repo.create(_job())
    orchestrator, _, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.error_code is None


async def test_a_new_revision_is_still_reviewed(repo) -> None:
    """The skip must key on the SHA, not merely on the issue having been seen."""
    await _completed_review_of_the_default_revision(repo)
    job = await repo.create(_job())
    orchestrator, github, _ = _build(repo, pull=_pull_request(head_sha="a-newer-sha"))

    await orchestrator.execute(job)

    assert job.status is not ReviewJobStatus.SKIPPED
    assert github.comments != []


async def test_a_retry_attempt_bypasses_the_skip(repo) -> None:
    """Retrying is an explicit request to review this revision again."""
    await _completed_review_of_the_default_revision(repo)
    job = await repo.create(
        ReviewJob(idempotency_key="retry:x:2", linear_issue_id="issue-1", attempt_number=2)
    )
    orchestrator, github, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.status is not ReviewJobStatus.SKIPPED
    assert github.comments != []


# --- Sandbox + agent stage (optional enrichment) -----------------------------


async def test_deterministic_path_is_unchanged_when_agent_and_sandbox_are_not_wired(repo) -> None:
    """The default constructor call — no sandbox_id, no agent involvement."""
    job = await repo.create(_job())
    orchestrator, _, _ = _build(repo)

    await orchestrator.execute(job)

    assert job.sandbox_id is None
    assert job.result.findings == []
    assert job.result.ai_analysis_ran is False


async def test_agent_findings_feed_the_risk_engine_and_the_published_labels(repo) -> None:
    """A finding in an area the paths didn't reveal must still move the score."""
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    finding = Finding(
        category=Area.PAYMENTS,
        severity=Severity.HIGH,
        description="Stripe webhook signature not verified.",
    )
    agent = FakeAgentRunner(ReviewAnalysis(summary="Payments issue found.", findings=[finding]))
    orchestrator, github, _ = _build_with_agent(
        repo, FakeSandboxFactory(sandbox), agent, pull=_pull_request(files=["README.md"])
    )

    await orchestrator.execute(job)

    assert Area.PAYMENTS in job.result.areas
    assert "area: payments" in github.applied
    assert job.result.findings == [finding]
    assert job.result.ai_analysis_ran is True
    # README alone is LOW; the payments finding must be what pushes it up.
    assert job.risk_level is not RiskLevel.LOW


async def test_bare_analysis_areas_without_a_finding_do_not_move_the_score_or_labels(repo) -> None:
    """`ReviewAnalysis.areas` is an unsubstantiated claim; only `Finding.category` counts."""
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    # The agent claims PAYMENTS as an area but backs it with no finding.
    agent = FakeAgentRunner(ReviewAnalysis(summary="Nothing notable.", areas=[Area.PAYMENTS], findings=[]))
    orchestrator, github, _ = _build_with_agent(
        repo, FakeSandboxFactory(sandbox), agent, pull=_pull_request(files=["README.md"])
    )

    await orchestrator.execute(job)

    assert job.risk_level is RiskLevel.LOW
    assert Area.PAYMENTS not in job.result.areas
    assert "area: payments" not in github.applied


async def test_failed_specialists_are_recorded_on_the_published_result(repo) -> None:
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    agent = FakeAgentRunner(failed_specialists=["security", "testing"])
    orchestrator, _, _ = _build_with_agent(
        repo, FakeSandboxFactory(sandbox), agent, pull=_pull_request(files=["README.md"])
    )

    await orchestrator.execute(job)

    assert job.result.failed_specialists == ["security", "testing"]


async def test_a_failed_specialist_forces_human_review_even_on_an_otherwise_low_risk_pr(repo) -> None:
    """A gap in analysis coverage is exactly the situation a human should be
    told about, independent of what score the deterministic engine reached."""
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    agent = FakeAgentRunner(failed_specialists=["architecture"])
    orchestrator, github, _ = _build_with_agent(
        repo, FakeSandboxFactory(sandbox), agent, pull=_pull_request(files=["README.md"])
    )

    await orchestrator.execute(job)

    assert job.risk_level is RiskLevel.LOW
    assert job.result.needs_human_review is True
    assert "review: needs-human" in github.applied


async def test_no_failed_specialists_does_not_force_human_review(repo) -> None:
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    agent = FakeAgentRunner(failed_specialists=[])
    orchestrator, _, _ = _build_with_agent(
        repo, FakeSandboxFactory(sandbox), agent, pull=_pull_request(files=["README.md"])
    )

    await orchestrator.execute(job)

    assert job.result.needs_human_review is False


async def test_sandbox_lifecycle_calls_happen_in_order(repo) -> None:
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    agent = FakeAgentRunner()
    orchestrator, _, _ = _build_with_agent(repo, FakeSandboxFactory(sandbox), agent)

    await orchestrator.execute(job)

    assert job.sandbox_id == sandbox.id
    assert sandbox.uploaded[0][0] == "archive.tar.gz"
    assert sandbox.uploaded[0][1] == b"fake-archive-bytes"
    assert any("tar -xzf" in call for call in sandbox.exec_calls)
    assert sandbox.destroyed is True
    assert agent.analyze_calls[0][1] is sandbox


async def test_archive_is_downloaded_at_the_exact_head_sha(repo) -> None:
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    agent = FakeAgentRunner()
    orchestrator, github, _ = _build_with_agent(
        repo, FakeSandboxFactory(sandbox), agent, pull=_pull_request(head_sha="exact-sha-123")
    )

    await orchestrator.execute(job)

    assert github.downloaded == (REPO, "exact-sha-123")


async def test_sandbox_is_destroyed_even_when_the_agent_raises(repo) -> None:
    job = await repo.create(_job())
    sandbox = FakeSandbox()
    agent = FakeAgentRunner(raise_error=RuntimeError("model call failed"))
    orchestrator, _, _ = _build_with_agent(repo, FakeSandboxFactory(sandbox), agent)

    with pytest.raises(RuntimeError):
        await orchestrator.execute(job)

    assert sandbox.destroyed is True


async def test_sandbox_is_destroyed_when_extraction_fails(repo) -> None:
    from app.core.errors import AidaMateError

    job = await repo.create(_job())
    sandbox = FakeSandbox()
    sandbox.script_extract_failure()
    agent = FakeAgentRunner()
    orchestrator, _, _ = _build_with_agent(repo, FakeSandboxFactory(sandbox), agent)

    with pytest.raises(AidaMateError, match="Archive extraction failed"):
        await orchestrator.execute(job)

    assert sandbox.destroyed is True


async def test_sandbox_unavailable_fails_the_whole_review_when_agent_is_configured(repo) -> None:
    """Once wired, a broken sandbox fails the review rather than silently
    degrading to areas-only scoring."""
    from app.core.errors import SandboxUnavailableError

    job = await repo.create(_job())
    factory = FakeSandboxFactory(FakeSandbox(), fail=True)
    agent = FakeAgentRunner()
    orchestrator, github, _ = _build_with_agent(repo, factory, agent)

    with pytest.raises(SandboxUnavailableError):
        await orchestrator.execute(job)

    assert github.comments == []  # never reached publication


async def test_invalid_pr_is_raised_for_a_closed_pull_request(repo) -> None:
    from app.core.errors import InvalidPullRequestError

    job = await repo.create(_job())
    github = FakeGitHub(PullRequest(
        ref=REF, title="x", base_ref="main", head_ref="x", head_sha="abc", state="closed",
        changed_files=[ChangedFile(filename="a.py", status=FileChangeStatus.MODIFIED)],
    ))
    orchestrator = ReviewOrchestrator(
        linear=FakeLinear(_issue()), resolver=build_resolver(github, [REPO]),
        github=github, repository=repo,
    )

    with pytest.raises(InvalidPullRequestError):
        await orchestrator.execute(job)


async def test_invalid_pr_is_raised_for_zero_changed_files(repo) -> None:
    from app.core.errors import InvalidPullRequestError

    job = await repo.create(_job())
    github = FakeGitHub(PullRequest(
        ref=REF, title="x", base_ref="main", head_ref="x", head_sha="abc", changed_files=[],
    ))
    orchestrator = ReviewOrchestrator(
        linear=FakeLinear(_issue()), resolver=build_resolver(github, [REPO]),
        github=github, repository=repo,
    )

    with pytest.raises(InvalidPullRequestError):
        await orchestrator.execute(job)


async def test_valid_open_pr_with_files_is_not_rejected(repo) -> None:
    """The guard must not be so strict it rejects the normal case."""
    job = await repo.create(_job())
    orchestrator, _, _ = _build(repo)

    await orchestrator.execute(job)  # must not raise InvalidPullRequestError

    assert job.result is not None
