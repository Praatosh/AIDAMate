"""Ensures every GitHub repo linked via GitHub Issue sync (CLAUDE.md §1c)
has at least one AIDA-MATE scheduled prompt watching it (CLAUDE.md §1d).

Bridges two independently-flagged features without coupling their control
flow: called from GitHubIssueSyncService right after a brand-new
SyncMapping is created, and reused as-is for the one-time backfill of
repos that were already linked before this shipped. Idempotent by design
— safe to call repeatedly for the same repo; only ever creates, never
duplicates or touches an existing schedule (auto-created or human-made).
"""

from app.core.interfaces import IScheduledPromptRepository
from app.core.logging import get_logger
from app.models.scheduled_prompt import ScheduledPrompt
from app.services.scheduled_prompt_dashboard_service import ScheduledPromptDashboardService

logger = get_logger(__name__)

DEFAULT_PROMPT = (
    "Run a general security and code quality check on this repository, "
    "and report anything worth a human's attention."
)
DEFAULT_RUN_AT_TIME = "09:00"
DEFAULT_TIMEZONE = "Asia/Kolkata"


class DefaultRepoScheduleService:
    """Creates a default daily scheduled prompt for a repository, once."""

    def __init__(
        self,
        scheduled_prompt_repository: IScheduledPromptRepository,
        dashboard_service: ScheduledPromptDashboardService,
        token_store,
    ) -> None:
        self._scheduled_prompts = scheduled_prompt_repository
        self._dashboard = dashboard_service
        self._token_store = token_store

    async def ensure_for_repository(self, repository: str) -> None:
        """No-op if `repository` already has any scheduled prompt at all
        (auto-created or human-made) — this only guarantees at-least-one."""
        existing = await self._scheduled_prompts.list_all()
        if any(s.repository == repository for s in existing):
            return

        installation = await self._token_store.get_default()
        if installation is None:
            logger.warning(
                "No default Linear installation; cannot create a default schedule",
                extra={"repository": repository},
            )
            return
        organization_id = installation.organization_id

        dashboard = await self._dashboard.ensure(organization_id)
        if dashboard is None:
            logger.warning(
                "Could not resolve the dashboard issue; cannot create a default schedule",
                extra={"repository": repository, "organization_id": organization_id},
            )
            return

        scheduled = ScheduledPrompt(
            title=f"Default check — {repository}",
            prompt=DEFAULT_PROMPT,
            repository=repository,
            frequency="daily",
            run_at_time=DEFAULT_RUN_AT_TIME,
            timezone=DEFAULT_TIMEZONE,
            linear_issue_id=dashboard.linear_issue_id,
            organization_id=organization_id,
        )
        await self._scheduled_prompts.create(scheduled)
        await self._dashboard.sync(organization_id)
        logger.info(
            "Created default scheduled prompt for a linked repository",
            extra={"repository": repository},
        )
