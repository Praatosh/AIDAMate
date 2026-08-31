"""The scheduled-prompt timer loop. See CLAUDE.md §1d.

Mirrors `app/workers/review_worker.py`'s `ReviewQueue` `asyncio.Task`
start/stop lifecycle, but the loop itself is time-driven rather than
queue-driven: nothing enqueues work here, a tick just checks the clock.

This is the first timer-based loop in AIDA-MATE — everything else in this
codebase reacts to an inbound webhook.
"""

import asyncio
import calendar
import contextlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.interfaces import IScheduledPromptRepository
from app.core.logging import get_logger
from app.models.scheduled_prompt import ScheduledPrompt
from app.services.scheduled_prompt_dashboard_service import ScheduledPromptDashboardService
from app.services.scheduled_prompt_service import ScheduledPromptService

logger = get_logger(__name__)

#: How often the loop wakes up to check for due schedules. A schedule's own
#: `run_at_time` is only "HH:MM" resolution, so a minute-granularity tick
#: cannot miss one — checked against `DEFAULT_TICK_INTERVAL_S` in
#: `test_scheduled_prompt_worker.py`.
DEFAULT_TICK_INTERVAL_S = 60.0

#: How often to re-sync every organization's dashboard regardless of
#: schedule activity — the only way a newly created Linear team picks up
#: its own dashboard issue without waiting on a schedule being created,
#: updated, or fired. Linear has no webhook event for team creation this
#: app can react to, so this is polling, not a push. Deliberately coarser
#: than `DEFAULT_TICK_INTERVAL_S`: a full resync costs one Linear call per
#: team in the organization (see CLAUDE.md §1d's cost note), so doing it
#: every tick would multiply that cost for no benefit when nothing changed.
DEFAULT_DASHBOARD_RESYNC_INTERVAL_S = 600.0


def _effective_day_of_month(now: datetime, day_of_month: int) -> int:
    """Clamp `day_of_month` to the current month's actual last day.

    A `day_of_month=31` schedule must still fire in a 30-day month rather
    than silently never matching that month.
    """
    last_day = calendar.monthrange(now.year, now.month)[1]
    return min(day_of_month, last_day)


def _is_due(scheduled: ScheduledPrompt, now: datetime) -> bool:
    """Whether `scheduled` should fire at `now` (already localized to its own timezone).

    Every frequency is judged from the single `last_run_at` timestamp — no
    per-frequency tracking field needed. `once` and the elapsed-time
    `hourly` case are judged without ever consulting `run_at_time`'s clock
    match beyond what each needs; `daily`/`weekly`/`monthly` all share the
    same "clock matches, and we haven't already fired in this period" shape.
    """
    if scheduled.frequency == "once":
        if scheduled.last_run_at is not None:
            return False  # already fired; never again regardless of clock match
        return (
            now.date().isoformat() == scheduled.run_on_date
            and now.strftime("%H:%M") == scheduled.run_at_time
        )

    if scheduled.frequency == "hourly":
        if scheduled.last_run_at is None:
            return True
        elapsed = now.astimezone(UTC) - scheduled.last_run_at
        return elapsed >= timedelta(hours=scheduled.interval_hours)

    if now.strftime("%H:%M") != scheduled.run_at_time:
        return False

    already_ran_this_period = False
    if scheduled.last_run_at is not None:
        last_local = scheduled.last_run_at.astimezone(now.tzinfo)
        if scheduled.frequency == "daily":
            already_ran_this_period = last_local.date() == now.date()
        elif scheduled.frequency == "weekly":
            already_ran_this_period = last_local.isocalendar()[:2] == now.isocalendar()[:2]
        elif scheduled.frequency == "monthly":
            already_ran_this_period = (last_local.year, last_local.month) == (now.year, now.month)
    if already_ran_this_period:
        return False

    if scheduled.frequency == "weekly":
        return now.weekday() == scheduled.day_of_week
    if scheduled.frequency == "monthly":
        return now.day == _effective_day_of_month(now, scheduled.day_of_month)
    return True  # daily


class ScheduledPromptWorker:
    """Ticks a clock and fires any `ScheduledPrompt` due at that instant."""

    def __init__(
        self,
        repository: IScheduledPromptRepository,
        service: ScheduledPromptService,
        dashboard_service: ScheduledPromptDashboardService | None = None,
        *,
        tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
        dashboard_resync_interval_s: float = DEFAULT_DASHBOARD_RESYNC_INTERVAL_S,
    ) -> None:
        self._repository = repository
        self._service = service
        self._dashboard_service = dashboard_service
        self._tick_interval_s = tick_interval_s
        self._dashboard_resync_interval_s = dashboard_resync_interval_s
        self._last_dashboard_resync_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the timer loop. Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="scheduled-prompt-worker")
        logger.info("Scheduled prompt worker started", extra={"tick_interval_s": self._tick_interval_s})

    async def stop(self) -> None:
        """Cancel the timer loop and wait for it to unwind."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("Scheduled prompt worker stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval_s)
            try:
                await self.tick()
            except Exception:
                # `tick()` already guards each individual schedule's run and
                # dashboard sync with their own try/except — this is a
                # last-resort net for a failure in the tick machinery itself
                # (e.g. `list_all()` raising on a transient SQLite error).
                # Without it, an unhandled exception here would kill this
                # `asyncio.Task` permanently: nothing awaits it, so nothing
                # would ever notice, and no schedule would fire again until a
                # manual restart. Mirrors `ReviewQueue._consume`'s identical
                # guard around `self._worker.run(job_id)`.
                logger.exception("Scheduled prompt worker tick failed unexpectedly")

    async def tick(self) -> None:
        """Check every schedule once and run any that are due.

        Entries run sequentially, not concurrently — acceptable at this
        project's scale (a handful of schedules, not hundreds).

        Each due entry claims the run instant (`mark_run` + `save`) *before*
        `service.run` executes, not after. That ordering — rather than a
        per-entry lock — is what makes a slow run safe: ticks are processed
        one at a time within this method, and `_loop`'s next `sleep` doesn't
        start until `tick()` returns, so there is no concurrent second tick
        to race against. A run that takes longer than one tick interval just
        means the next tick, whenever it comes, already sees this instant
        claimed and skips it — the same "prove the race, then prove the fix"
        class of bug this project hit twice before (`AutoMergeService`,
        `LinearAuthService`), solved here architecturally instead of with a
        lock, since ticks are already guaranteed non-overlapping.
        """
        already_synced: set[str] = set()
        for scheduled in await self._repository.list_all():
            if not scheduled.enabled:
                continue
            try:
                now = datetime.now(ZoneInfo(scheduled.timezone))
            except Exception:
                logger.warning(
                    "Scheduled prompt has an invalid timezone; skipping",
                    extra={"scheduled_prompt_id": scheduled.id, "timezone": scheduled.timezone},
                )
                continue
            if not _is_due(scheduled, now):
                continue

            scheduled.mark_run(now)
            if scheduled.frequency == "once":
                # A fired one-shot schedule is done for good — `_is_due`
                # already guarantees it can never re-fire once `last_run_at`
                # is set, so rather than leaving a disabled row lingering on
                # the dashboard, it's removed outright. Deleting here (before
                # the run, not after) is also what claims the run instant:
                # a schedule that no longer exists in the repository can't be
                # picked up by a later `list_all()` either, the same
                # race-safety property `save()` gave the old
                # mark-run-then-disable approach, just via absence instead of
                # a flag.
                await self._repository.delete(scheduled.id)
            else:
                await self._repository.save(scheduled)
            try:
                await self._service.run(scheduled)
            except Exception:
                # `service.run` already handles and reports its own errors;
                # this is a last-resort net so one bad entry can never take
                # down the whole loop.
                logger.exception(
                    "Scheduled prompt worker caught an unhandled error",
                    extra={"scheduled_prompt_id": scheduled.id},
                )

            # Fires whether the run above succeeded or failed — either way
            # `last_run_at` just changed, and the dashboard should reflect
            # it. `dashboard_service.sync` already never raises on its own,
            # but this still gets its own net for the same "one bad entry
            # can never take down the loop" reason as the block above.
            if self._dashboard_service is not None and scheduled.organization_id is not None:
                try:
                    await self._dashboard_service.sync(scheduled.organization_id)
                    already_synced.add(scheduled.organization_id)
                except Exception:
                    logger.exception(
                        "Scheduled prompt worker caught an unhandled dashboard-sync error",
                        extra={"scheduled_prompt_id": scheduled.id},
                    )

        await self._maybe_resync_dashboards(skip=already_synced)

    async def _maybe_resync_dashboards(self, *, skip: set[str]) -> None:
        """Re-sync every organization's dashboard on a coarse timer, independent
        of whether any schedule fired this tick. See `DEFAULT_DASHBOARD_RESYNC_INTERVAL_S`
        for why this exists — it's how a newly created Linear team gets its own
        dashboard issue without a human having to touch a schedule first.

        `skip` holds organization ids already synced earlier in this same
        `tick()` call (a schedule of theirs just fired) — resyncing them again
        a few lines later would be redundant, not incorrect.
        """
        if self._dashboard_service is None:
            return
        now = datetime.now(UTC)
        if self._last_dashboard_resync_at is not None and (
            now - self._last_dashboard_resync_at < timedelta(seconds=self._dashboard_resync_interval_s)
        ):
            return
        self._last_dashboard_resync_at = now

        organization_ids = {s.organization_id for s in await self._repository.list_all() if s.organization_id}
        for organization_id in organization_ids - skip:
            try:
                await self._dashboard_service.sync(organization_id)
            except Exception:
                logger.exception(
                    "Scheduled prompt worker caught an unhandled dashboard-resync error",
                    extra={"organization_id": organization_id},
                )
