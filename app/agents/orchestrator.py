"""Review pipeline orchestration.

A plain async function, not an LLM tool-calling loop. The stage order
(context → resolve → fetch → sandbox → analyze → classify → publish) has
exactly one valid sequence, since each stage consumes the previous stage's
output. Handing an LLM authority over a fixed-order DAG would add
skip/reorder/retry failure modes and buy nothing — in the layer that most
needs to be predictable.

The OpenAI Agents SDK remains the only agent framework in the system; every
genuine reasoning step runs through it. Only sequencing is fixed Python. That
"genuine reasoning step" is itself, internally, a multi-agent pipeline
(Context, four concurrent specialists, a Judge — see `agents/review_agent.py`)
sequenced by the same discipline, one level down: still plain Python, never
an LLM-controlled handoff. None of that is visible from here — `analyze()`
still returns one `AgentRunOutcome` wrapping one `ReviewAnalysis`, exactly as
when a single agent produced it.

The full pipeline is implemented: context fetch, PR resolution, PR fetch,
deduplication, area detection, risk scoring, label derivation, publication to
GitHub and Linear — and, when a sandbox factory and agent runner are wired in,
sandboxed AI analysis that contributes *findings* on top of the deterministic
area detection. The agent can enrich a review; it cannot change the risk
level, which stays the risk engine's decision alone (see `core/risk_engine.py`
and design decision #4 in the project's Docker Sandbox integration plan:
only `Finding.category` — never the model's own `ReviewAnalysis.areas` claim
— feeds the score).

Sandbox and agent are optional constructor arguments. Without them, the
pipeline runs exactly as before — deterministic-only, no sandbox, no model —
the same pattern already used for an unconfigured `github_service`.
"""

import asyncio

from app.core.area_detector import detect_areas
from app.core.errors import AgentTimeoutError, AidaMateError, InvalidPullRequestError
from app.core.events import (
    AGENT_COMPLETED,
    AGENT_STARTED,
    ANALYSIS_COMPLETED,
    ANALYSIS_REUSED,
    GITHUB_UPDATED,
    LINEAR_UPDATED,
    PR_FETCHED,
    REVIEW_SKIPPED_DUPLICATE,
    RISK_CLASSIFIED,
    SANDBOX_CREATED,
    SANDBOX_DESTROYED,
    SANDBOX_READY,
)
from app.core.interfaces import IReviewAgentRunner, ISandboxFactory
from app.core.label_engine import EXCLUSIVE_NAMESPACES, LABEL_NEEDS_HUMAN, build_labels, label_catalog
from app.core.logging import get_logger
from app.core.report import (
    GITHUB_COMMENT_MARKER,
    render_github_comment,
    render_linear_comment,
)
from app.core.risk_engine import RiskThresholds, assess_risk, is_owasp_relevant
from app.models.common import ReviewJobStatus
from app.models.github import PullRequest
from app.models.linear import AgentActivityType
from app.models.review import (
    AgentRunOutcome,
    Finding,
    ReviewJob,
    ReviewResult,
    build_content_key,
)
from app.services.job_repository import InMemoryReviewJobRepository
from app.services.linear_service import LinearService
from app.services.pr_resolver import PullRequestResolver
from app.tools.sandbox_tools import SANDBOX_REPO_DIR

logger = get_logger(__name__)

#: Where the downloaded archive lands in the sandbox workspace before
#: extraction, and where it's extracted from — both relative to the
#: workspace root that `SbxSandbox.exec()` now defaults `cwd` to.
_ARCHIVE_FILENAME = "archive.tar.gz"

#: GitHub tarballs wrap their content in one top-level `owner-repo-sha/`
#: directory; stripping it is what makes in-sandbox paths match
#: `ChangedFile.filename` (e.g. `app/auth/middleware.py`, not
#: `acme-api-3f2c9ab/app/auth/middleware.py`). Standard GitHub tarball
#: layout — not one of the `sbx` CLI facts verified this session,
#: worth a real-download sanity check before relying on it in production.
_EXTRACT_COMMAND = (
    f"mkdir -p {SANDBOX_REPO_DIR} && "
    f"tar -xzf {_ARCHIVE_FILENAME} -C {SANDBOX_REPO_DIR} --strip-components=1"
)


class ReviewOrchestrator:
    """Drives a review job through the analysis pipeline."""

    def __init__(
        self,
        linear: LinearService,
        resolver: PullRequestResolver,
        github,
        repository: InMemoryReviewJobRepository,
        thresholds: RiskThresholds | None = None,
        sandbox_factory: ISandboxFactory | None = None,
        agent_runner: IReviewAgentRunner | None = None,
        agent_timeout_s: float = 300.0,
        sandbox_mode: str | None = None,
    ) -> None:
        self._linear = linear
        self._resolver = resolver
        self._github = github
        self._repository = repository
        self._thresholds = thresholds or RiskThresholds()
        self._sandbox_factory = sandbox_factory
        self._agent_runner = agent_runner
        self._agent_timeout_s = agent_timeout_s
        #: Published alongside the result so a reader never has to take
        #: "isolated in Docker" on faith — see the `local` backend's own
        #: docstring for why that distinction matters.
        self._sandbox_mode = sandbox_mode

    async def execute(self, job: ReviewJob) -> None:
        """Run the pipeline for `job`, mutating it as stages complete.

        Raises:
            AidaMateError: on any stage failure. The worker records it and
                reports the safe message back to the requester. Once a
                sandbox/agent is configured, a failure in that stage fails the
                whole review rather than silently degrading to areas-only
                scoring — an operator who turned the capability on should be
                told when it breaks, not have it quietly vanish.
        """
        job.mark_status(ReviewJobStatus.FETCHING_PR)
        await self._repository.save(job)

        issue = await self._linear.get_issue(job.linear_issue_id, organization_id=job.organization_id)
        job.linear_issue_identifier = issue.identifier or job.linear_issue_identifier

        # Resolution happens before any sandbox exists, so an issue with no
        # linked PR costs two API calls rather than a provisioned environment.
        pull_request_ref = await self._resolver.resolve(issue)
        job.pull_request = pull_request_ref
        await self._repository.save(job)

        pull_request = await self._github.get_pull_request(pull_request_ref)
        job.head_sha = pull_request.head_sha
        await self._repository.save(job)

        if pull_request.state != "open" or not pull_request.changed_files:
            raise InvalidPullRequestError(
                f"{pull_request_ref.slug} is not reviewable "
                f"(state={pull_request.state}, changed_files={len(pull_request.changed_files)})"
            )

        logger.info(
            PR_FETCHED,
            extra={
                **job.log_context(),
                "event": PR_FETCHED,
                "changed_files": len(pull_request.changed_files),
                "additions": pull_request.total_additions,
                "deletions": pull_request.total_deletions,
                "is_fork": pull_request.is_fork,
            },
        )

        if await self._skip_if_already_reviewed(job):
            return

        job.mark_status(ReviewJobStatus.ANALYZING)
        await self._repository.save(job)

        # Deterministic classification runs unconditionally: areas come from
        # file paths, independent of whether the agent stage is wired up.
        detection = detect_areas(pull_request.changed_files)

        # Not `self._agent_runner is not None` alone: that only says a runner was
        # *constructed*, which happens independently of sandbox availability. What
        # published text and logs must reflect is whether the agent actually ran.
        ai_analysis_ran = self._sandbox_factory is not None and self._agent_runner is not None

        findings: list[Finding] = []
        ai_model: str | None = None
        tool_calls_count = 0
        failed_specialists: list[str] = []
        analysis_reused = False
        if ai_analysis_ran:
            reused = await self._find_reusable_analysis(job)
            if reused is not None:
                findings = list(reused.findings)
                ai_model = reused.ai_model
                tool_calls_count = reused.tool_calls_count
                failed_specialists = list(reused.failed_specialists)
                analysis_reused = True
                logger.info(
                    ANALYSIS_REUSED,
                    extra={**job.log_context(), "event": ANALYSIS_REUSED, "findings": len(findings)},
                )
            else:
                outcome = await self._run_agent_analysis(job, pull_request)
                findings = outcome.analysis.findings
                ai_model = outcome.model
                tool_calls_count = outcome.tool_calls_count
                failed_specialists = outcome.failed_specialists

        logger.info(
            ANALYSIS_COMPLETED,
            extra={
                **job.log_context(),
                "event": ANALYSIS_COMPLETED,
                "ai_analysis_ran": ai_analysis_ran,
                "findings": len(findings),
                "failed_specialists": failed_specialists,
            },
        )

        job.mark_status(ReviewJobStatus.CLASSIFYING)
        await self._repository.save(job)

        assessment = assess_risk(detection.areas, findings=findings, thresholds=self._thresholds)

        # Only what actually scored feeds areas/labels/OWASP — never the
        # model's own unsubstantiated `analysis.areas` claim. See the module
        # docstring: this is what keeps "the LLM cannot move the risk level"
        # true for the *label set* too, not just the numeric score.
        effective_areas = {contribution.area for contribution in assessment.breakdown}
        labels = build_labels(assessment, effective_areas)

        # A gap in analysis coverage is exactly the situation a human should
        # be told about — this OR is plain Python on a count of failed agent
        # *names*, never influenced by anything a model emits, so it cannot
        # be gamed the way finding count could. `build_labels` only knows the
        # deterministic engine's own signal, so the label is reconciled with
        # the final decision here rather than changing that function's
        # well-tested, engine-only contract.
        needs_human_review = assessment.needs_human_review or bool(failed_specialists)
        if needs_human_review:
            labels.add(LABEL_NEEDS_HUMAN)

        job.risk_level = assessment.level
        job.result = ReviewResult(
            risk=assessment.level,
            risk_score=assessment.score,
            needs_human_review=needs_human_review,
            labels=sorted(labels),
            areas=sorted(effective_areas, key=lambda area: area.value),
            security_impact=is_owasp_relevant(effective_areas),
            owasp_relevant=is_owasp_relevant(effective_areas),
            summary=_build_summary(pull_request_ref.slug, detection, assessment),
            findings=findings,
            breakdown=assessment.breakdown,
            ai_analysis_ran=ai_analysis_ran,
            ai_model=ai_model,
            tool_calls_count=tool_calls_count,
            sandbox_mode=self._sandbox_mode if ai_analysis_ran else None,
            failed_specialists=failed_specialists,
            analysis_reused=analysis_reused,
        )
        await self._repository.save(job)

        logger.info(
            RISK_CLASSIFIED,
            extra={
                **job.log_context(),
                "event": RISK_CLASSIFIED,
                "risk_score": assessment.score,
                "needs_human_review": needs_human_review,
                "areas": [area.value for area in job.result.areas],
                "labels": job.result.labels,
            },
        )

        # The result is persisted above, before anything is published. A GitHub
        # or Linear outage must not destroy a completed analysis — with the
        # result stored, publication is retryable on its own.
        await self._publish_to_github(job, pull_request_ref, labels)
        await self._publish_to_linear(job, pull_request_ref)

    async def _find_reusable_analysis(self, job: ReviewJob) -> ReviewResult | None:
        """Reuse a previous COMPLETED review's AI findings for this exact revision, if any.

        `_skip_if_already_reviewed` deliberately lets explicit assignment/delegation through
        even when the revision is unchanged, so that re-delegating always publishes a fresh
        comment. But the AI stage is not perfectly reproducible run-to-run — a second pass
        over the same diff can surface a different finding set and therefore a different
        score, which breaks the reproducibility `risk_engine.py` promises. Reusing the prior
        run's findings for an unchanged `content_key` keeps the score identical across repeat
        re-delegations and skips a second sandbox + six-agent run for a revision nothing new
        happened to. A genuinely new commit gets its own `content_key` and always gets a fresh
        run.
        """
        if job.content_key is None:
            return None
        previous = await self._repository.find_completed_by_content_key(job.content_key, exclude=job.id)
        if previous is None or previous.result is None or not previous.result.ai_analysis_ran:
            return None
        return previous.result

    async def _run_agent_analysis(self, job: ReviewJob, pull_request: PullRequest) -> AgentRunOutcome:
        """Provision a sandbox, extract the PR, run the agent, always clean up.

        Raises on any failure in this stage — see the "once configured, a
        sandbox/agent failure fails the whole review" note on `execute()`.
        Cleanup (`sandbox.destroy()`) is unconditional via `finally`.
        """
        sandbox = await self._sandbox_factory.create(labels={"review_id": job.id})
        job.sandbox_id = sandbox.id
        await self._repository.save(job)
        logger.info(SANDBOX_CREATED, extra={**job.log_context(), "event": SANDBOX_CREATED})

        try:
            archive = await self._github.download_archive(
                pull_request.ref.repository, pull_request.head_sha
            )
            await sandbox.upload_bytes(_ARCHIVE_FILENAME, archive)

            extract_result = await sandbox.exec(_EXTRACT_COMMAND, timeout_s=self._agent_timeout_s)
            if extract_result.exit_code != 0:
                raise AidaMateError(
                    f"Archive extraction failed (exit {extract_result.exit_code}): "
                    f"{extract_result.stderr[:500]}"
                )
            logger.info(SANDBOX_READY, extra={**job.log_context(), "event": SANDBOX_READY})

            logger.info(AGENT_STARTED, extra={**job.log_context(), "event": AGENT_STARTED})
            try:
                outcome = await asyncio.wait_for(
                    self._agent_runner.analyze(pull_request, sandbox, review_id=job.id),
                    timeout=self._agent_timeout_s,
                )
            except TimeoutError as exc:
                raise AgentTimeoutError(
                    f"Agent analysis exceeded {self._agent_timeout_s}s"
                ) from exc

            logger.info(
                AGENT_COMPLETED,
                extra={
                    **job.log_context(),
                    "event": AGENT_COMPLETED,
                    "findings": len(outcome.analysis.findings),
                    "tool_calls_count": outcome.tool_calls_count,
                    "model": outcome.model,
                },
            )
            return outcome
        finally:
            await sandbox.destroy()
            logger.info(SANDBOX_DESTROYED, extra={**job.log_context(), "event": SANDBOX_DESTROYED})

    async def _publish_to_github(self, job: ReviewJob, ref, labels: set[str]) -> None:
        """Apply labels and post the review summary onto the pull request."""
        job.mark_status(ReviewJobStatus.UPDATING_GITHUB)
        await self._repository.save(job)

        await self._github.ensure_labels_exist(ref.repository, label_catalog(labels))
        await self._github.apply_labels(ref, labels, exclusive_prefixes=EXCLUSIVE_NAMESPACES)
        await self._github.add_comment(
            ref,
            render_github_comment(job.result, ref.slug),
            marker=GITHUB_COMMENT_MARKER,
        )

        logger.info(GITHUB_UPDATED, extra={**job.log_context(), "event": GITHUB_UPDATED})

    async def _publish_to_linear(self, job: ReviewJob, ref) -> None:
        """Mirror the verdict back onto the Linear issue.

        A failure here does not fail the review: the developer already has the
        result on the pull request, which is where the work happens. Losing the
        status echo is a far smaller harm than reporting a completed review as
        failed.
        """
        job.mark_status(ReviewJobStatus.UPDATING_LINEAR)
        await self._repository.save(job)

        body = render_linear_comment(job.result, ref.url, ref.slug)

        try:
            if job.agent_session_id:
                # An agent session is the richer surface: the result lands as a
                # session activity rather than a detached comment.
                await self._linear.emit_agent_activity(
                    job.agent_session_id,
                    AgentActivityType.RESPONSE,
                    body,
                    organization_id=job.organization_id,
                )
            else:
                await self._linear.add_comment(
                    job.linear_issue_id, body, organization_id=job.organization_id
                )
            logger.info(LINEAR_UPDATED, extra={**job.log_context(), "event": LINEAR_UPDATED})
        except Exception as exc:
            logger.warning(
                "Could not update Linear; the review itself succeeded",
                extra={**job.log_context(), "error": type(exc).__name__},
            )

    async def _skip_if_already_reviewed(self, job: ReviewJob) -> bool:
        """Skip duplicate automatic reviews of an unchanged PR revision.

        Automatic/background triggers are deduplicated by content. Explicit
        assignment and delegation are always treated as a fresh request, even
        when the PR head SHA is unchanged.

        This runs after the PR fetch because the head SHA is not knowable
        before it. The three GitHub calls that precede it are the floor on what
        a duplicate trigger can cost; everything expensive is downstream.

        Explicit assignment/delegation and retries bypass this check. Delivery
        and in-flight idempotency still collapse duplicate webhook deliveries
        before reaching this point.

        Returns:
            True if the caller should stop; False to continue reviewing.
        """
        if job.pull_request is None:
            return False

        job.content_key = build_content_key(
            job.linear_issue_id, job.pull_request.number, job.head_sha
        )
        await self._repository.save(job)

        if job.attempt_number > 1 or job.trigger_source in {"agent_session", "issue_assignment"}:
            return False

        previous = await self._repository.find_completed_by_content_key(job.content_key, exclude=job.id)
        if previous is None:
            return False

        job.mark_skipped(
            f"This pull request revision was already reviewed (review {previous.id})."
        )
        await self._repository.save(job)
        logger.info(
            REVIEW_SKIPPED_DUPLICATE,
            extra={
                **job.log_context(),
                "event": REVIEW_SKIPPED_DUPLICATE,
                "previous_review_id": previous.id,
            },
        )
        return True


def _build_summary(slug: str, detection, assessment) -> str:
    """Render the developer-facing summary.

    Kept concise on purpose: a wall of model output is ignored, and the point of
    the summary is to make the verdict and its justification obvious at a glance.
    """
    areas = ", ".join(sorted(area.value for area in detection.areas)) or "none detected"
    contributions = ", ".join(
        f"{contribution.area.value} +{contribution.points}" for contribution in assessment.breakdown
    )

    lines = [
        f"Risk **{assessment.level.value}** (score {assessment.score}) for `{slug}`.",
        f"Affected areas: {areas}.",
    ]
    if contributions:
        lines.append(f"Score breakdown: {contributions}.")
    if assessment.needs_human_review:
        lines.append("Human review is required before merging.")
    if detection.test_only:
        lines.append("This pull request changes tests only.")
    return " ".join(lines)
