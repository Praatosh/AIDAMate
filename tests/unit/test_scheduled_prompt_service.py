"""ScheduledPromptService: resolve -> sandbox -> analyze -> post, and its
never-raise failure-reporting guarantee (CLAUDE.md §1d).

Small recording fakes for github/sandbox-factory/prompt-runner/linear, same
style as `test_github_merge_sync_service.py`.
"""

from dataclasses import dataclass, field

import pytest

from app.core.errors import AgentError, GitHubError
from app.models.scheduled_prompt import ScheduledPrompt
from app.services.scheduled_prompt_service import ScheduledPromptService


def _scheduled(**overrides) -> ScheduledPrompt:
    values = {
        "title": "Security audit",
        "prompt": "Run a general security audit of this repository.",
        "repository": "acme/api",
        "run_at_time": "09:00",
        "timezone": "Asia/Kolkata",
        "linear_issue_id": "issue-1",
        "organization_id": "org-1",
    }
    values.update(overrides)
    return ScheduledPrompt(**values)


@dataclass
class _ExecResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeSandbox:
    id = "fake-sandbox"

    def __init__(self, *, extract_exit_code: int = 0) -> None:
        self.uploaded: list[tuple[str, bytes]] = []
        self.extract_calls: list[tuple[str, str]] = []
        self.destroyed = False
        self._extract_exit_code = extract_exit_code

    async def upload_bytes(self, dest_path: str, content: bytes) -> None:
        self.uploaded.append((dest_path, content))

    async def extract_archive(self, archive_path: str, dest_dir: str) -> _ExecResult:
        self.extract_calls.append((archive_path, dest_dir))
        stderr = "extraction failed" if self._extract_exit_code else ""
        return _ExecResult(exit_code=self._extract_exit_code, stderr=stderr)

    async def read_file(self, path: str, *, max_bytes: int = 200_000) -> str:
        raise NotImplementedError

    async def destroy(self) -> None:
        self.destroyed = True


class FakeSandboxFactory:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self._sandbox = sandbox
        self.create_labels: dict | None = None

    async def create(self, *, labels=None):
        self.create_labels = labels
        return self._sandbox


@dataclass
class FakeGitHub:
    default_branch: str = "main"
    sha: str = "abc123"
    pr_head_sha: str = "pr-sha-456"
    archive: bytes = b"archive-bytes"
    fail_default_branch: bool = False
    fail_commit_sha: bool = False
    fail_pr_head_sha: bool = False
    fail_download: bool = False
    default_branch_calls: int = field(default=0, init=False)
    commit_sha_calls: int = field(default=0, init=False)
    pr_head_sha_calls: list = field(default_factory=list, init=False)

    async def get_default_branch(self, repo) -> str:
        self.default_branch_calls += 1
        if self.fail_default_branch:
            raise GitHubError("could not resolve default branch")
        return self.default_branch

    async def get_commit_sha(self, repo, ref: str) -> str:
        self.commit_sha_calls += 1
        if self.fail_commit_sha:
            raise GitHubError("could not resolve commit sha")
        return self.sha

    async def get_pull_request_head_sha(self, ref) -> str:
        self.pr_head_sha_calls.append(ref.number)
        if self.fail_pr_head_sha:
            raise GitHubError("could not resolve pull request head sha")
        return self.pr_head_sha

    async def download_archive(self, repo, sha: str) -> bytes:
        if self.fail_download:
            raise GitHubError("could not download archive")
        return self.archive


class FakePromptRunner:
    def __init__(self, *, output: str = "## Findings\nAll clear.", fail: bool = False) -> None:
        self._output = output
        self._fail = fail
        self.calls: list[tuple[str, object]] = []

    async def run(self, prompt: str, sandbox, *, timeout_s, review_id) -> str:
        self.calls.append((prompt, sandbox))
        if self._fail:
            raise AgentError("agent run failed")
        return self._output


class FakeLinear:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.comments: list[tuple[str, str, str | None]] = []

    async def add_comment(self, issue_id: str, body: str, *, organization_id=None) -> None:
        if self._fail:
            raise GitHubError("Linear unreachable")  # any exception works for the swallow test
        self.comments.append((issue_id, body, organization_id))


@pytest.fixture
def sandbox() -> FakeSandbox:
    return FakeSandbox()


async def test_run_resolves_default_branch_downloads_and_posts(sandbox: FakeSandbox) -> None:
    github = FakeGitHub()
    factory = FakeSandboxFactory(sandbox)
    prompt_runner = FakePromptRunner(output="## Findings\nNothing concerning.")
    linear = FakeLinear()
    service = ScheduledPromptService(github, factory, prompt_runner, linear)
    scheduled = _scheduled()

    await service.run(scheduled)

    assert github.default_branch_calls == 1
    assert sandbox.uploaded == [("archive.tar.gz", b"archive-bytes")]
    assert sandbox.destroyed is True
    assert len(prompt_runner.calls) == 1
    assert prompt_runner.calls[0][0] == scheduled.prompt
    assert len(linear.comments) == 1
    issue_id, body, org_id = linear.comments[0]
    assert issue_id == "issue-1"
    assert org_id == "org-1"
    assert "Nothing concerning." in body
    assert scheduled.prompt in body


async def test_run_with_explicit_branch_skips_default_branch_resolution(sandbox: FakeSandbox) -> None:
    github = FakeGitHub()
    factory = FakeSandboxFactory(sandbox)
    service = ScheduledPromptService(github, factory, FakePromptRunner(), FakeLinear())

    await service.run(_scheduled(branch="develop"))

    assert github.default_branch_calls == 0


async def test_run_with_pr_number_uses_the_prs_head_sha_and_skips_branch_resolution(
    sandbox: FakeSandbox,
) -> None:
    github = FakeGitHub()
    factory = FakeSandboxFactory(sandbox)
    linear = FakeLinear()
    service = ScheduledPromptService(github, factory, FakePromptRunner(), linear)

    await service.run(_scheduled(pr_number=123))

    assert github.pr_head_sha_calls == [123]
    assert github.default_branch_calls == 0
    assert github.commit_sha_calls == 0
    assert sandbox.uploaded == [("archive.tar.gz", b"archive-bytes")]
    issue_id, body, org_id = linear.comments[0]
    assert "PR #123" in body


async def test_run_with_pr_number_ignores_an_explicit_branch(sandbox: FakeSandbox) -> None:
    """`pr_number` wins over `branch` when both happen to be set."""
    github = FakeGitHub()
    factory = FakeSandboxFactory(sandbox)
    service = ScheduledPromptService(github, factory, FakePromptRunner(), FakeLinear())

    await service.run(_scheduled(branch="develop", pr_number=123))

    assert github.pr_head_sha_calls == [123]
    assert github.default_branch_calls == 0
    assert github.commit_sha_calls == 0


async def test_pr_head_sha_failure_posts_a_failure_comment(sandbox: FakeSandbox) -> None:
    github = FakeGitHub(fail_pr_head_sha=True)
    factory = FakeSandboxFactory(sandbox)
    linear = FakeLinear()
    service = ScheduledPromptService(github, factory, FakePromptRunner(), linear)

    await service.run(_scheduled(pr_number=123))  # must not raise

    assert len(linear.comments) == 1
    assert "failed" in linear.comments[0][1].lower()


async def test_sandbox_is_labeled_with_the_schedules_id(sandbox: FakeSandbox) -> None:
    github = FakeGitHub()
    factory = FakeSandboxFactory(sandbox)
    service = ScheduledPromptService(github, factory, FakePromptRunner(), FakeLinear())
    scheduled = _scheduled()

    await service.run(scheduled)

    assert factory.create_labels == {"scheduled_prompt_id": scheduled.id}


# --- Failure paths: never raise, always report to Linear ---------------------


async def test_extract_failure_posts_a_failure_comment_and_still_destroys_the_sandbox() -> None:
    sandbox = FakeSandbox(extract_exit_code=1)
    github = FakeGitHub()
    factory = FakeSandboxFactory(sandbox)
    linear = FakeLinear()
    service = ScheduledPromptService(github, factory, FakePromptRunner(), linear)

    await service.run(_scheduled())  # must not raise

    assert sandbox.destroyed is True
    assert len(linear.comments) == 1
    assert "failed" in linear.comments[0][1].lower()


async def test_download_failure_posts_a_failure_comment_and_still_destroys_the_sandbox(
    sandbox: FakeSandbox,
) -> None:
    github = FakeGitHub(fail_download=True)
    factory = FakeSandboxFactory(sandbox)
    linear = FakeLinear()
    service = ScheduledPromptService(github, factory, FakePromptRunner(), linear)

    await service.run(_scheduled())  # must not raise

    assert sandbox.destroyed is True
    assert len(linear.comments) == 1
    assert "failed" in linear.comments[0][1].lower()


async def test_branch_resolution_failure_posts_a_failure_comment_without_creating_a_sandbox(
    sandbox: FakeSandbox,
) -> None:
    github = FakeGitHub(fail_default_branch=True)
    factory = FakeSandboxFactory(sandbox)
    linear = FakeLinear()
    service = ScheduledPromptService(github, factory, FakePromptRunner(), linear)

    await service.run(_scheduled())  # must not raise

    assert factory.create_labels is None
    assert len(linear.comments) == 1


async def test_prompt_runner_failure_posts_a_failure_comment(sandbox: FakeSandbox) -> None:
    github = FakeGitHub()
    factory = FakeSandboxFactory(sandbox)
    linear = FakeLinear()
    service = ScheduledPromptService(github, factory, FakePromptRunner(fail=True), linear)

    await service.run(_scheduled())  # must not raise

    assert sandbox.destroyed is True
    assert len(linear.comments) == 1
    assert "failed" in linear.comments[0][1].lower()


async def test_a_failure_that_also_cannot_be_reported_to_linear_still_never_raises(
    sandbox: FakeSandbox,
) -> None:
    """Both the run and the failure-comment attempt fail — the last-resort net."""
    github = FakeGitHub(fail_download=True)
    factory = FakeSandboxFactory(sandbox)
    linear = FakeLinear(fail=True)
    service = ScheduledPromptService(github, factory, FakePromptRunner(), linear)

    await service.run(_scheduled())  # must not raise
