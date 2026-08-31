"""AIDA-MATE FastAPI application and composition root.

This module is the only place that knows which concrete adapters back which
interfaces. Everything downstream depends on the Protocols in
`app/core/interfaces.py`, which is what keeps the GitHub / model-provider /
sandbox swaps cheap.

The sandbox (`sbx` CLI, Docker Sandboxes) and the review agent (OpenAI Agents SDK)
are both optional: without them, `ReviewOrchestrator` still runs its full
deterministic area/risk/label pipeline — the same pattern already used for an
unconfigured `github_service`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.agents.orchestrator import ReviewOrchestrator
from app.agents.prompt_runner import ScheduledPromptRunner
from app.agents.review_agent import OpenAIAgentRunner
from app.api import (
    comment_deletion,
    github_webhook,
    health,
    linear_auth,
    linear_webhook,
    merge_confirmation,
    reviews,
    scheduled_prompt_form,
    scheduled_prompts,
)
from app.core.config import ModelProvider, SandboxMode, Settings, get_settings
from app.core.errors import AidaMateError
from app.core.interfaces import IReviewAgentRunner, ISandboxFactory
from app.core.logging import get_logger, setup_logging
from app.core.risk_engine import RiskThresholds
from app.models.github import RepositoryRef
from app.services.auto_merge_service import AutoMergeService
from app.services.default_schedule_service import DefaultRepoScheduleService
from app.services.github_issue_sync_service import GitHubIssueSyncService
from app.services.github_merge_sync_service import GitHubMergeSyncService
from app.services.github_service import (
    GitHubAppCredentials,
    GitHubService,
    StaticTokenCredentials,
)
from app.services.job_repository import InMemoryReviewJobRepository
from app.services.linear_auth_service import LinearAuthService
from app.services.linear_service import LinearGraphQLClient, LinearService
from app.services.local_sandbox_service import LocalSandboxFactory
from app.services.posted_comment_repository import InMemoryPostedCommentRepository
from app.services.pr_resolver import build_resolver
from app.services.review_service import ReviewService
from app.services.sandbox_service import SbxSandboxFactory
from app.services.scheduled_prompt_dashboard_repository import InMemoryScheduledPromptDashboardRepository
from app.services.scheduled_prompt_dashboard_service import ScheduledPromptDashboardService
from app.services.scheduled_prompt_repository import InMemoryScheduledPromptRepository
from app.services.scheduled_prompt_service import ScheduledPromptService
from app.services.sqlite_job_repository import SqliteReviewJobRepository
from app.services.sqlite_posted_comment_repository import SqlitePostedCommentRepository
from app.services.sqlite_scheduled_prompt_dashboard_repository import (
    SqliteScheduledPromptDashboardRepository,
)
from app.services.sqlite_scheduled_prompt_repository import SqliteScheduledPromptRepository
from app.services.sqlite_sync_mapping_repository import SqliteSyncMappingRepository
from app.services.sync_mapping_repository import InMemorySyncMappingRepository
from app.services.token_store import FileLinearTokenStore, InMemoryLinearTokenStore, InMemoryOAuthStateStore
from app.workers.review_worker import (
    PendingPipelineExecutor,
    ReviewQueue,
    ReviewWorker,
)
from app.workers.scheduled_prompt_worker import ScheduledPromptWorker

#: Bound on outbound calls to Linear/GitHub so a hung dependency cannot pin a
#: worker indefinitely.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

logger = get_logger(__name__)


def _log_startup_capabilities(settings: Settings) -> None:
    """Log what AIDA-MATE can and cannot currently do.

    Operators should learn about a missing capability at boot, not from a
    review that silently failed an hour later.
    """
    agent_enabled = settings.model_provider is ModelProvider.OPENAI and bool(settings.openai_api_key)

    logger.info(
        "AIDA-MATE starting up",
        extra={
            "app_env": settings.app_env,
            "log_level": settings.log_level,
            "model_provider": settings.model_provider.value,
            "review_model": settings.review_model,
            "sandbox_mode": settings.sandbox_mode.value,
            "sandbox_configured": settings.sandbox_configured,
            "agent_enabled": agent_enabled,
            "github_app_configured": settings.github_app_configured,
            "linear_oauth_configured": settings.linear_oauth_configured,
        },
    )

    if not settings.sandbox_configured:
        logger.warning(
            "No sandbox available ('sbx' not on PATH, or Docker Desktop isn't running). "
            "Reviews will run the deterministic area/risk/label pipeline without AI-generated "
            "findings — install Docker Desktop and run 'sbx login' once to enable it."
        )
    elif not agent_enabled:
        logger.warning(
            "Sandbox is available, but the configured model provider has no runner "
            "implementation; reviews will run deterministic-only.",
            extra={"model_provider": settings.model_provider.value},
        )
    if not (settings.github_app_configured or settings.github_dev_token):
        logger.warning("No GitHub credentials configured; PR retrieval will fail.")
    no_github_credentials = not (settings.github_app_configured or settings.github_dev_token)
    if settings.auto_merge_on_done_enabled and no_github_credentials:
        logger.warning(
            "AUTO_MERGE_ON_DONE_ENABLED is set, but no GitHub credentials are configured; "
            "the gated auto-merge action is enabled but structurally inert."
        )
    if not (settings.linear_oauth_configured or settings.linear_dev_api_key):
        logger.warning("No Linear credentials configured; issue reads and result writes will fail.")
    if not settings.aida_mate_linear_actor_id:
        logger.warning("AIDA_MATE_LINEAR_ACTOR_ID is not set; assignment events cannot be detected.")
    if settings.github_merge_sync_enabled and not settings.github_webhook_secret:
        logger.warning(
            "GITHUB_MERGE_SYNC_ENABLED is set, but GITHUB_WEBHOOK_SECRET is unset; every GitHub "
            "webhook delivery will be rejected as unsigned — the sync is enabled but structurally inert."
        )
    if settings.github_issue_sync_enabled and not settings.github_webhook_secret:
        logger.warning(
            "GITHUB_ISSUE_SYNC_ENABLED is set, but GITHUB_WEBHOOK_SECRET is unset; every GitHub "
            "webhook delivery will be rejected as unsigned — the sync is enabled but structurally inert."
        )
    if settings.github_issue_sync_enabled and not settings.linear_sync_team_key:
        logger.warning(
            "GITHUB_ISSUE_SYNC_ENABLED is set, but LINEAR_SYNC_TEAM_KEY is unset; synced issues have "
            "nowhere to be created — the sync is enabled but structurally inert."
        )
    if settings.scheduled_prompts_enabled and (
        no_github_credentials or not settings.sandbox_configured or not agent_enabled
    ):
        logger.warning(
            "SCHEDULED_PROMPTS_ENABLED is set, but GitHub credentials, a sandbox backend, and an "
            "agent runner are not all configured; the scheduled-prompt worker is enabled but "
            "structurally inert."
        )
    if settings.scheduled_prompts_enabled and not settings.linear_sync_team_key:
        logger.warning(
            "SCHEDULED_PROMPTS_ENABLED is set, but LINEAR_SYNC_TEAM_KEY is unset; the scheduled-"
            "prompts dashboard issue has no team to be created under and will not sync."
        )
    if not settings.management_api_key:
        logger.warning(
            "MANAGEMENT_API_KEY is not set; every request to /reviews* and /scheduled-prompts* "
            "will be rejected with 401 (fail-closed, not fail-open — see app/core/api_auth.py). "
            "Set MANAGEMENT_API_KEY and send it as an X-Api-Key header to use these endpoints."
        )
    if not settings.review_store_path:
        logger.warning(
            "Review jobs are in-memory only; a restart loses all review state and interrupted "
            "reviews cannot be recovered or retried. Set REVIEW_STORE_PATH to persist them."
        )
    if settings.linear_token_store_path:
        logger.info(
            "Linear OAuth installations persist to a local file; not encrypted at rest — "
            "development convenience only.",
            extra={"path": settings.linear_token_store_path},
        )
    else:
        logger.warning(
            "Linear OAuth installations are in-memory only; every restart requires every "
            "workspace to reconnect. Set LINEAR_TOKEN_STORE_PATH for local-dev persistence."
        )


def _build_github_service(settings: Settings, http_client: httpx.AsyncClient) -> GitHubService | None:
    """Construct the GitHub client, or None if no credentials are configured.

    The App is preferred; a development token is a fallback. Returning None
    rather than a half-configured client keeps the failure at startup, where it
    is visible, instead of inside a review.
    """
    if settings.github_app_configured:
        credentials = GitHubAppCredentials(
            app_id=settings.github_app_id,
            installation_id=settings.github_installation_id,
            private_key=settings.github_private_key,
            http_client=http_client,
            api_url=settings.github_api_url,
        )
    elif settings.github_dev_token:
        credentials = StaticTokenCredentials(settings.github_dev_token)
    else:
        return None

    return GitHubService(credentials, http_client, api_url=settings.github_api_url)


def _build_sandbox_factory(settings: Settings) -> ISandboxFactory | None:
    """Construct the sandbox factory, or None if no backend is available.

    None is a supported state, not a startup failure: the deterministic
    pipeline works without a sandbox and never depended on one.
    """
    if not settings.sandbox_configured:
        return None
    if settings.sandbox_mode is SandboxMode.LOCAL:
        logger.warning(
            "SANDBOX_MODE=local: PR content is extracted to a host temp directory instead of an "
            "isolated Docker Sandbox. Read-only (list/read/grep) only, nothing from the PR is ever "
            "executed — but this is NOT an isolation boundary. Use only where Docker Sandboxes "
            "genuinely cannot run."
        )
        return LocalSandboxFactory(workdir_root=None, default_timeout_s=settings.sandbox_timeout_seconds)
    return SbxSandboxFactory(
        binary=settings.sandbox_binary,
        workdir_root=None,
        default_timeout_s=settings.sandbox_timeout_seconds,
    )


def _build_agent_runner(settings: Settings) -> IReviewAgentRunner | None:
    """Construct the review agent runner for the configured model provider.

    Only OpenAI has a concrete implementation today (`anthropic` isn't
    installed in this environment). Any other provider — or OpenAI without a
    key — falls through to None, same as a missing sandbox: the deterministic
    pipeline still runs, just without agent-contributed findings.
    """
    if settings.model_provider is ModelProvider.OPENAI and settings.openai_api_key:
        return OpenAIAgentRunner(
            model=settings.review_model,
            utility_model=settings.utility_model,
            reasoning_effort=settings.model_reasoning_effort,
            specialist_timeout_s=settings.specialist_timeout_seconds,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            api_version=settings.openai_api_version,
        )
    return None


def _build_scheduled_prompt_runner(settings: Settings) -> ScheduledPromptRunner | None:
    """Construct the scheduled-prompt agent runner, same provider gating as `_build_agent_runner`."""
    if settings.model_provider is ModelProvider.OPENAI and settings.openai_api_key:
        return ScheduledPromptRunner(
            model=settings.review_model,
            reasoning_effort=settings.model_reasoning_effort,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            api_version=settings.openai_api_version,
        )
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the object graph at startup and tear it down at shutdown.

    Configuration is validated first: a missing required secret stops the
    process here with a clear message rather than surfacing later as a
    confusing mid-review failure.
    """
    try:
        settings = get_settings()
    except ValidationError as exc:
        setup_logging("ERROR")
        logger.error("Invalid configuration; refusing to start", extra={"errors": exc.errors()})
        raise

    setup_logging(settings.log_level)
    _log_startup_capabilities(settings)

    # One shared client so connections are pooled across all outbound calls.
    http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)

    linear_token_store = (
        FileLinearTokenStore(settings.linear_token_store_path)
        if settings.linear_token_store_path
        else InMemoryLinearTokenStore()
    )
    linear_graphql = LinearGraphQLClient(http_client)
    linear_auth_service = LinearAuthService(
        settings=settings,
        token_store=linear_token_store,
        state_store=InMemoryOAuthStateStore(),
        http_client=http_client,
    )
    # Built before linear_service so it can be injected into it below. Same
    # settings.review_store_path-conditional in-memory/SQLite split as
    # sync_mapping_repository/scheduled_prompt_repository further down, own
    # table. Always wired (no feature flag) — every comment AIDA-MATE posts
    # gets a delete link.
    posted_comment_repository = (
        SqlitePostedCommentRepository(settings.review_store_path)
        if settings.review_store_path
        else InMemoryPostedCommentRepository()
    )
    linear_service = LinearService(
        linear_graphql,
        linear_auth_service,
        posted_comment_repository=posted_comment_repository,
        base_url=settings.public_base_url,
    )

    job_repository = (
        SqliteReviewJobRepository(settings.review_store_path)
        if settings.review_store_path
        else InMemoryReviewJobRepository()
    )
    # Anything left non-terminal belongs to a process that died mid-review.
    # Marking it INTERRUPTED here is what makes it visible to the retry path
    # instead of sitting in ANALYZING forever.
    await job_repository.recover_interrupted()

    # Same conditional as job_repository: shares its SQLite file when one is
    # configured, own table, own schema (CLAUDE.md §1c).
    sync_mapping_repository = (
        SqliteSyncMappingRepository(settings.review_store_path)
        if settings.review_store_path
        else InMemorySyncMappingRepository()
    )
    # Same conditional again, own table, own schema (CLAUDE.md §1d).
    scheduled_prompt_repository = (
        SqliteScheduledPromptRepository(settings.review_store_path)
        if settings.review_store_path
        else InMemoryScheduledPromptRepository()
    )
    # Same conditional again, own table, own schema — the dashboard's
    # organization -> Linear-issue mapping (CLAUDE.md §1d).
    scheduled_prompt_dashboard_repository = (
        SqliteScheduledPromptDashboardRepository(settings.review_store_path)
        if settings.review_store_path
        else InMemoryScheduledPromptDashboardRepository()
    )
    # Built here (rather than alongside scheduled_prompt_service below) so
    # default_schedule_service, and in turn github_issue_sync_service, can
    # both depend on it. Only needs Linear + the team key — independent of
    # the GitHub/sandbox/agent-runner gating scheduled_prompt_service needs,
    # since a dashboard can exist even before any schedule has ever
    # successfully run (CLAUDE.md §1d).
    scheduled_prompt_dashboard_service = (
        ScheduledPromptDashboardService(
            scheduled_prompt_dashboard_repository,
            scheduled_prompt_repository,
            linear_service,
            team_key=settings.linear_sync_team_key,
            base_url=settings.public_base_url,
        )
        if settings.linear_sync_team_key
        else None
    )
    # Bridges §1c and §1d (CLAUDE.md): ensures every repo GitHub Issue sync
    # touches also gets a default scheduled prompt. Gated on
    # scheduled_prompts_enabled too, not just the dashboard being
    # configured — no point auto-creating schedules the worker won't run.
    default_schedule_service = (
        DefaultRepoScheduleService(
            scheduled_prompt_repository, scheduled_prompt_dashboard_service, linear_token_store
        )
        if settings.scheduled_prompts_enabled and scheduled_prompt_dashboard_service is not None
        else None
    )

    # Built once, shared by the review pipeline and the scheduled-prompt
    # service below — both need a sandbox, and a factory is stateless.
    sandbox_factory = _build_sandbox_factory(settings)

    github_service = _build_github_service(settings, http_client)
    if github_service is None:
        # Without GitHub credentials nothing can be resolved or fetched, so the
        # placeholder stays in place and every job fails with a clear reason
        # rather than erroring deep inside the pipeline.
        executor = PendingPipelineExecutor()
    else:
        repositories = [
            RepositoryRef(owner=owner, name=name)
            for owner, _, name in (entry.partition("/") for entry in settings.github_repos)
        ]
        executor = ReviewOrchestrator(
            linear=linear_service,
            resolver=build_resolver(github_service, repositories),
            github=github_service,
            repository=job_repository,
            thresholds=RiskThresholds.from_settings(settings),
            sandbox_factory=sandbox_factory,
            agent_runner=_build_agent_runner(settings),
            agent_timeout_s=settings.agent_timeout_seconds,
            sandbox_mode=settings.sandbox_mode.value,
        )

    auto_merge_service = (
        AutoMergeService(job_repository, github_service, linear_service, base_url=settings.public_base_url)
        if github_service is not None
        else None
    )
    # Unlike auto_merge_service, this never calls GitHub — it only reads
    # webhook payloads and writes to Linear — so it needs no GitHub
    # credentials and is always constructed.
    github_merge_sync_service = GitHubMergeSyncService(job_repository, linear_service)
    # Needs GitHub reads (commit->PR / PR-search lookups), same conditional
    # as auto_merge_service, unlike github_merge_sync_service above.
    github_issue_sync_service = (
        GitHubIssueSyncService(
            sync_mapping_repository,
            github_service,
            linear_service,
            team_key=settings.linear_sync_team_key or "",
            default_schedule_service=default_schedule_service,
        )
        if github_service is not None
        else None
    )
    # Needs GitHub reads AND a sandbox AND an agent runner, unlike every
    # other optional service above — a scheduled prompt has nothing useful
    # to do without all three (CLAUDE.md §1d).
    scheduled_prompt_runner = _build_scheduled_prompt_runner(settings)
    scheduled_prompt_ready = (
        github_service is not None and sandbox_factory is not None and scheduled_prompt_runner is not None
    )
    scheduled_prompt_service = (
        ScheduledPromptService(github_service, sandbox_factory, scheduled_prompt_runner, linear_service)
        if scheduled_prompt_ready
        else None
    )

    review_queue = ReviewQueue(
        ReviewWorker(job_repository, executor, linear_service),
        concurrency=settings.review_concurrency,
        maxsize=settings.review_queue_maxsize,
    )
    await review_queue.start()

    scheduled_prompt_worker = (
        ScheduledPromptWorker(
            scheduled_prompt_repository, scheduled_prompt_service, scheduled_prompt_dashboard_service
        )
        if settings.scheduled_prompts_enabled and scheduled_prompt_service is not None
        else None
    )
    if scheduled_prompt_worker is not None:
        await scheduled_prompt_worker.start()

    app.state.settings = settings
    app.state.http_client = http_client
    app.state.job_repository = job_repository
    app.state.linear_token_store = linear_token_store
    app.state.linear_auth_service = linear_auth_service
    app.state.linear_service = linear_service
    app.state.posted_comment_repository = posted_comment_repository
    app.state.github_service = github_service
    app.state.auto_merge_service = auto_merge_service
    app.state.github_merge_sync_service = github_merge_sync_service
    app.state.github_issue_sync_service = github_issue_sync_service
    app.state.scheduled_prompt_repository = scheduled_prompt_repository
    app.state.scheduled_prompt_service = scheduled_prompt_service
    app.state.scheduled_prompt_dashboard_service = scheduled_prompt_dashboard_service
    app.state.scheduled_prompt_worker = scheduled_prompt_worker
    app.state.review_queue = review_queue
    app.state.review_service = ReviewService(job_repository, review_queue)

    try:
        yield
    finally:
        if scheduled_prompt_worker is not None:
            await scheduled_prompt_worker.stop()
        await review_queue.stop()
        await http_client.aclose()
        logger.info("AIDA-MATE shutting down")


app = FastAPI(
    title="AIDA-MATE",
    description=(
        "AI-powered Pull Request Risk Analysis Agent. Triggered by assigning a Linear issue to "
        "AIDA-MATE; analyzes the linked GitHub PR in an isolated sandbox and classifies its risk "
        "using a deterministic engine."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(linear_webhook.router)
app.include_router(linear_auth.router)
app.include_router(reviews.router)
app.include_router(merge_confirmation.router)
app.include_router(comment_deletion.router)
app.include_router(github_webhook.router)
# Registered before scheduled_prompts.router: its literal "/scheduled-prompts/new"
# path must be matched before that router's "/scheduled-prompts/{scheduled_id}"
# pattern would otherwise treat "new" as an id.
app.include_router(scheduled_prompt_form.router)
app.include_router(scheduled_prompts.router)


@app.exception_handler(AidaMateError)
async def aida_mate_error_handler(request: Request, exc: AidaMateError) -> JSONResponse:
    """Return the developer-safe message for known domain failures.

    `user_message` is curated to be free of secrets and internal detail, so it
    is safe to expose; the underlying exception is logged in full.
    """
    logger.warning(
        "Domain error while processing request",
        extra={"path": request.url.path, "error_code": exc.code, "detail": str(exc)},
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": exc.code, "detail": exc.user_message},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the full exception, return a generic body.

    Unexpected failures must never leak stack traces or secret-bearing error
    text to external callers such as Linear's webhook delivery system.
    """
    logger.exception("Unhandled exception while processing request", extra={"path": request.url.path})
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": "Internal server error"},
    )
