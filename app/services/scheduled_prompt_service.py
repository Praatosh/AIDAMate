"""Runs one scheduled prompt: resolve -> sandbox -> analyze -> post. See CLAUDE.md §1d.

Mirrors `ReviewOrchestrator._run_agent_analysis`'s sandbox-provisioning
sequence (`app/agents/orchestrator.py`), simplified: no `ReviewJob`, no status
transitions, no risk classification — a scheduled prompt has none of those.
"""

from app.agents.prompt_runner import ScheduledPromptRunner
from app.core.errors import AidaMateError
from app.core.interfaces import ISandboxFactory
from app.core.logging import get_logger
from app.models.github import PullRequestRef, RepositoryRef
from app.models.scheduled_prompt import ScheduledPrompt
from app.services.github_service import GitHubService
from app.services.linear_service import LinearService
from app.tools.sandbox_tools import SANDBOX_REPO_DIR

logger = get_logger(__name__)

_ARCHIVE_FILENAME = "archive.tar.gz"

#: Bounds both the extract command and the agent run — the same value
#: `ReviewOrchestrator` uses `agent_timeout_s` for on the extract step, and
#: what the agent itself is bounded by here.
_RUN_TIMEOUT_SECONDS = 300.0


def _repository_ref(repository: str) -> RepositoryRef:
    owner, name = repository.split("/", 1)
    return RepositoryRef(owner=owner, name=name)


def _target_label(scheduled: ScheduledPrompt) -> str:
    """The repository (and PR, if this schedule targets one) a run studied."""
    if scheduled.pr_number is not None:
        return f"`{scheduled.repository}` PR #{scheduled.pr_number}"
    return f"`{scheduled.repository}`"


def _render(scheduled: ScheduledPrompt, output: str) -> str:
    """Render a scheduled run's output for posting as a Linear comment.

    Deliberately minimal: the prompt that was actually run (so it's
    unambiguous which schedule produced this comment when several post to
    the same issue) and what it studied, then the agent's own already-terse
    findings with no extra framing around them. No timestamp line — Linear
    already shows each comment's own posted-at time natively.
    """
    return f'**Prompt:** "{scheduled.prompt}" — {_target_label(scheduled)}\n\n{output}'


def _render_failure(scheduled: ScheduledPrompt, error: Exception) -> str:
    return f'**Prompt:** "{scheduled.prompt}" — {_target_label(scheduled)}\n\nFailed: {error}'


class ScheduledPromptService:
    """Executes one `ScheduledPrompt` end to end."""

    def __init__(
        self,
        github: GitHubService,
        sandbox_factory: ISandboxFactory,
        prompt_runner: ScheduledPromptRunner,
        linear: LinearService,
    ) -> None:
        self._github = github
        self._sandbox_factory = sandbox_factory
        self._prompt_runner = prompt_runner
        self._linear = linear

    async def run(self, scheduled: ScheduledPrompt) -> None:
        """Run `scheduled` once: download a repo snapshot, run the prompt, post the result.

        Never raises: called from the scheduler worker's tick loop, which must
        keep running regardless of one schedule's outcome. Any failure is
        logged and reported back to Linear as a short failure comment instead
        — the same "the operator finds out either way" guarantee a completed
        run gives via its own comment.
        """
        repo = _repository_ref(scheduled.repository)
        try:
            if scheduled.pr_number is not None:
                ref = PullRequestRef(
                    repository=repo,
                    number=scheduled.pr_number,
                    url=f"https://github.com/{scheduled.repository}/pull/{scheduled.pr_number}",
                )
                sha = await self._github.get_pull_request_head_sha(ref)
            else:
                branch = scheduled.branch or await self._github.get_default_branch(repo)
                sha = await self._github.get_commit_sha(repo, branch)

            sandbox = await self._sandbox_factory.create(labels={"scheduled_prompt_id": scheduled.id})
            try:
                archive = await self._github.download_archive(repo, sha)
                await sandbox.upload_bytes(_ARCHIVE_FILENAME, archive)
                extract_result = await sandbox.extract_archive(_ARCHIVE_FILENAME, SANDBOX_REPO_DIR)
                if extract_result.exit_code != 0:
                    raise AidaMateError(
                        f"Failed to extract repository archive: {extract_result.stderr}"
                    )
                output = await self._prompt_runner.run(
                    scheduled.prompt,
                    sandbox,
                    timeout_s=_RUN_TIMEOUT_SECONDS,
                    review_id=scheduled.id,
                )
            finally:
                await sandbox.destroy()

            await self._linear.add_comment(
                scheduled.linear_issue_id,
                _render(scheduled, output),
                organization_id=scheduled.organization_id,
            )
            logger.info(
                "Scheduled prompt ran successfully",
                extra={"scheduled_prompt_id": scheduled.id, "repository": scheduled.repository},
            )
        except Exception as exc:
            logger.exception(
                "Scheduled prompt run failed",
                extra={"scheduled_prompt_id": scheduled.id, "repository": scheduled.repository},
            )
            try:
                await self._linear.add_comment(
                    scheduled.linear_issue_id,
                    _render_failure(scheduled, exc),
                    organization_id=scheduled.organization_id,
                )
            except Exception:
                logger.exception(
                    "Could not report scheduled prompt failure to Linear",
                    extra={"scheduled_prompt_id": scheduled.id},
                )
