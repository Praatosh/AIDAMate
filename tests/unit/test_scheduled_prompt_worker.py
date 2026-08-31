"""ScheduledPromptWorker: due-detection per frequency, no-double-fire, and
the claim-before-execute ordering that makes a slow run safe without a
per-entry lock (CLAUDE.md §1d).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.scheduled_prompt import ScheduledPrompt
from app.services.scheduled_prompt_repository import InMemoryScheduledPromptRepository
from app.workers.scheduled_prompt_worker import ScheduledPromptWorker, _is_due


def _scheduled(**overrides) -> ScheduledPrompt:
    values = {
        "title": "Security audit",
        "prompt": "Run a general security audit of this repository.",
        "repository": "acme/api",
        "frequency": "daily",
        "run_at_time": "09:00",
        "timezone": "Asia/Kolkata",
        "linear_issue_id": "issue-1",
    }
    values.update(overrides)
    return ScheduledPrompt(**values)


class FakeService:
    """Records every `run()` call, and can be made to sleep first to
    simulate a slow run overlapping with the next tick."""

    def __init__(self, *, sleep_s: float = 0.0) -> None:
        self._sleep_s = sleep_s
        self.runs: list[str] = []

    async def run(self, scheduled: ScheduledPrompt) -> None:
        if self._sleep_s:
            await asyncio.sleep(self._sleep_s)
        self.runs.append(scheduled.id)


class RaisingService:
    async def run(self, scheduled: ScheduledPrompt) -> None:
        raise RuntimeError("boom")


class FakeDashboardService:
    """Records every `sync()` call instead of touching Linear."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.synced: list[str] = []

    async def sync(self, organization_id: str) -> None:
        if self._fail:
            raise RuntimeError("dashboard sync boom")
        self.synced.append(organization_id)


# --- _is_due: once ---------------------------------------------------------------


def test_once_due_on_matching_date_and_time() -> None:
    now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="once", run_on_date="2026-08-24", run_at_time="09:00")

    assert _is_due(scheduled, now) is True


def test_once_not_due_on_a_different_date() -> None:
    now = datetime(2026, 8, 25, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="once", run_on_date="2026-08-24", run_at_time="09:00")

    assert _is_due(scheduled, now) is False


def test_once_never_fires_again_after_it_has_run() -> None:
    now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(
        frequency="once", run_on_date="2026-08-24", run_at_time="09:00", last_run_at=now.astimezone(UTC)
    )

    assert _is_due(scheduled, now) is False


# --- _is_due: hourly ---------------------------------------------------------------


def test_hourly_due_when_never_run() -> None:
    now = datetime(2026, 8, 24, 9, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="hourly", interval_hours=2, run_at_time=None)

    assert _is_due(scheduled, now) is True


def test_hourly_not_due_before_the_interval_elapses() -> None:
    now = datetime(2026, 8, 24, 9, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    last_run = (now - timedelta(hours=1)).astimezone(UTC)
    scheduled = _scheduled(frequency="hourly", interval_hours=2, run_at_time=None, last_run_at=last_run)

    assert _is_due(scheduled, now) is False


def test_hourly_due_once_the_interval_elapses() -> None:
    now = datetime(2026, 8, 24, 9, 17, tzinfo=ZoneInfo("Asia/Kolkata"))
    last_run = (now - timedelta(hours=2)).astimezone(UTC)
    scheduled = _scheduled(frequency="hourly", interval_hours=2, run_at_time=None, last_run_at=last_run)

    assert _is_due(scheduled, now) is True


# --- _is_due: daily ---------------------------------------------------------------


def test_daily_due_when_time_matches_and_not_yet_run_today() -> None:
    now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="daily", run_at_time="09:00", last_run_at=None)

    assert _is_due(scheduled, now) is True


def test_daily_not_due_when_time_does_not_match() -> None:
    now = datetime(2026, 8, 24, 9, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="daily", run_at_time="09:00")

    assert _is_due(scheduled, now) is False


def test_daily_not_due_when_already_run_today() -> None:
    now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    earlier_today = datetime(2026, 8, 24, 1, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="daily", run_at_time="09:00", last_run_at=earlier_today.astimezone(UTC))

    assert _is_due(scheduled, now) is False


def test_daily_due_again_on_a_new_day() -> None:
    now = datetime(2026, 8, 25, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    yesterday = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="daily", run_at_time="09:00", last_run_at=yesterday.astimezone(UTC))

    assert _is_due(scheduled, now) is True


# --- _is_due: weekly ---------------------------------------------------------------


def test_weekly_due_on_matching_weekday_and_time() -> None:
    now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Monday
    scheduled = _scheduled(frequency="weekly", day_of_week=0, run_at_time="09:00")

    assert _is_due(scheduled, now) is True


def test_weekly_not_due_on_a_different_weekday() -> None:
    now = datetime(2026, 8, 25, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Tuesday
    scheduled = _scheduled(frequency="weekly", day_of_week=0, run_at_time="09:00")

    assert _is_due(scheduled, now) is False


def test_weekly_not_due_if_already_run_this_week() -> None:
    now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Monday, ISO week 35
    earlier_same_week = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(
        frequency="weekly", day_of_week=0, run_at_time="09:00", last_run_at=earlier_same_week.astimezone(UTC)
    )

    assert _is_due(scheduled, now) is False


def test_weekly_due_again_next_week() -> None:
    now = datetime(2026, 8, 31, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Monday, ISO week 36
    last_week = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # ISO week 35
    scheduled = _scheduled(
        frequency="weekly", day_of_week=0, run_at_time="09:00", last_run_at=last_week.astimezone(UTC)
    )

    assert _is_due(scheduled, now) is True


# --- _is_due: monthly ---------------------------------------------------------------


def test_monthly_due_on_matching_day_and_time() -> None:
    now = datetime(2026, 8, 15, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="monthly", day_of_month=15, run_at_time="09:00")

    assert _is_due(scheduled, now) is True


def test_monthly_not_due_on_a_different_day() -> None:
    now = datetime(2026, 8, 16, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="monthly", day_of_month=15, run_at_time="09:00")

    assert _is_due(scheduled, now) is False


def test_monthly_clamps_to_the_months_last_day() -> None:
    """September has 30 days — day_of_month=31 must still fire on the 30th."""
    now = datetime(2026, 9, 30, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(frequency="monthly", day_of_month=31, run_at_time="09:00")

    assert _is_due(scheduled, now) is True


def test_monthly_not_due_if_already_run_this_month() -> None:
    now = datetime(2026, 8, 15, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    earlier_this_month = datetime(2026, 8, 15, 1, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(
        frequency="monthly",
        day_of_month=15,
        run_at_time="09:00",
        last_run_at=earlier_this_month.astimezone(UTC),
    )

    assert _is_due(scheduled, now) is False


def test_monthly_due_again_next_month() -> None:
    now = datetime(2026, 9, 15, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    last_month = datetime(2026, 8, 15, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    scheduled = _scheduled(
        frequency="monthly", day_of_month=15, run_at_time="09:00", last_run_at=last_month.astimezone(UTC)
    )

    assert _is_due(scheduled, now) is True


# --- tick(): due-detection across timezones -------------------------------------


async def test_tick_fires_a_schedule_whose_local_time_matches_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schedule in Asia/Kolkata (UTC+5:30) must fire at the right UTC instant."""
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr(
        "app.workers.scheduled_prompt_worker.datetime",
        _FixedDatetime(fixed_now),
    )

    repo = InMemoryScheduledPromptRepository()
    scheduled = await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata"))
    service = FakeService()
    worker = ScheduledPromptWorker(repo, service)

    await worker.tick()

    assert service.runs == [scheduled.id]


async def test_tick_skips_a_schedule_not_yet_due(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata"))
    service = FakeService()
    worker = ScheduledPromptWorker(repo, service)

    await worker.tick()

    assert service.runs == []


async def test_tick_skips_a_disabled_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", enabled=False))
    service = FakeService()
    worker = ScheduledPromptWorker(repo, service)

    await worker.tick()

    assert service.runs == []


async def test_tick_skips_a_schedule_with_an_invalid_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = InMemoryScheduledPromptRepository()
    scheduled = _scheduled(run_at_time="09:00", timezone="Asia/Kolkata")
    scheduled.timezone = "Not/AZone"  # bypass model validation to simulate corrupted state
    await repo.create(scheduled)
    service = FakeService()
    worker = ScheduledPromptWorker(repo, service)

    await worker.tick()  # must not raise

    assert service.runs == []


async def test_tick_marks_the_schedule_run_before_calling_service(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    scheduled = await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata"))
    worker = ScheduledPromptWorker(repo, FakeService())

    await worker.tick()

    saved = await repo.get(scheduled.id)
    assert saved.last_run_at == fixed_now.astimezone(UTC)


async def test_tick_deletes_a_once_schedule_after_it_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-shot schedule is removed outright once it fires, not just
    disabled — it should no longer exist, and so no longer show up on the
    dashboard, rather than lingering as a disabled row."""
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    scheduled = await repo.create(
        _scheduled(frequency="once", run_on_date="2026-08-24", run_at_time="09:00", timezone="Asia/Kolkata")
    )
    worker = ScheduledPromptWorker(repo, FakeService())

    await worker.tick()

    assert await repo.get(scheduled.id) is None
    assert await repo.list_all() == []


async def test_tick_deletes_a_once_schedule_even_when_the_run_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The schedule already "executed" (fired) whether or not the run itself
    succeeded — deletion shouldn't depend on the outcome, matching the
    existing dashboard-sync-regardless-of-outcome behavior below."""
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    scheduled = await repo.create(
        _scheduled(frequency="once", run_on_date="2026-08-24", run_at_time="09:00", timezone="Asia/Kolkata")
    )
    worker = ScheduledPromptWorker(repo, RaisingService())

    await worker.tick()

    assert await repo.get(scheduled.id) is None


async def test_tick_does_not_fire_twice_in_the_same_tick_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim-before-execute ordering: a second tick for the same instant
    must see the run already claimed and skip, even if the first run is slow."""
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    scheduled = await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata"))
    service = FakeService(sleep_s=0.05)
    worker = ScheduledPromptWorker(repo, service)

    first_tick = asyncio.create_task(worker.tick())
    await asyncio.sleep(0.01)  # let the first tick claim the run before the second starts
    await worker.tick()
    await first_tick

    assert service.runs == [scheduled.id]


async def test_a_failing_service_run_does_not_crash_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata"))
    worker = ScheduledPromptWorker(repo, RaisingService())

    await worker.tick()  # must not raise


# --- tick(): dashboard sync --------------------------------------------------


async def test_tick_syncs_the_dashboard_after_a_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    dashboard = FakeDashboardService()
    worker = ScheduledPromptWorker(repo, FakeService(), dashboard)

    await worker.tick()

    assert dashboard.synced == ["org-1"]


async def test_tick_syncs_the_dashboard_after_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed run still changed `last_run_at` — the dashboard should reflect that."""
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    dashboard = FakeDashboardService()
    worker = ScheduledPromptWorker(repo, RaisingService(), dashboard)

    await worker.tick()

    assert dashboard.synced == ["org-1"]


async def test_tick_does_not_sync_when_no_dashboard_service_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    worker = ScheduledPromptWorker(repo, FakeService())  # dashboard_service defaults to None

    await worker.tick()  # must not raise


async def test_tick_does_not_sync_when_the_schedule_has_no_organization_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata"))  # organization_id=None
    dashboard = FakeDashboardService()
    worker = ScheduledPromptWorker(repo, FakeService(), dashboard)

    await worker.tick()

    assert dashboard.synced == []


async def test_a_failing_dashboard_sync_does_not_crash_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    worker = ScheduledPromptWorker(repo, FakeService(), FakeDashboardService(fail=True))

    await worker.tick()  # must not raise


# --- tick(): periodic dashboard resync (picks up newly created Linear teams) -----


async def test_tick_resyncs_every_organizations_dashboard_even_with_no_due_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is what makes a brand-new Linear team's dashboard issue appear
    without a human having to touch a schedule first — see
    DEFAULT_DASHBOARD_RESYNC_INTERVAL_S."""
    fixed_now = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # not due (run_at_time=09:00)
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-2"))
    dashboard = FakeDashboardService()
    worker = ScheduledPromptWorker(repo, FakeService(), dashboard)

    await worker.tick()

    assert sorted(dashboard.synced) == ["org-1", "org-2"]


async def test_tick_does_not_resync_dashboards_again_before_the_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    fixed_dt = _FixedDatetime(fixed_now)
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", fixed_dt)

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    dashboard = FakeDashboardService()
    worker = ScheduledPromptWorker(repo, FakeService(), dashboard, dashboard_resync_interval_s=600)

    await worker.tick()
    await worker.tick()  # same fixed instant, well within the interval

    assert dashboard.synced == ["org-1"]


async def test_tick_resyncs_dashboards_again_once_the_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    fixed_dt = _FixedDatetime(fixed_now)
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", fixed_dt)

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    dashboard = FakeDashboardService()
    worker = ScheduledPromptWorker(repo, FakeService(), dashboard, dashboard_resync_interval_s=600)

    await worker.tick()
    fixed_dt._fixed = fixed_now + timedelta(seconds=601)
    await worker.tick()

    assert dashboard.synced == ["org-1", "org-1"]


async def test_a_failing_dashboard_resync_does_not_crash_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    worker = ScheduledPromptWorker(repo, FakeService(), FakeDashboardService(fail=True))

    await worker.tick()  # must not raise


async def test_dashboard_resync_is_skipped_when_no_dashboard_service_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    monkeypatch.setattr("app.workers.scheduled_prompt_worker.datetime", _FixedDatetime(fixed_now))

    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled(run_at_time="09:00", timezone="Asia/Kolkata", organization_id="org-1"))
    worker = ScheduledPromptWorker(repo, FakeService())  # dashboard_service defaults to None

    await worker.tick()  # must not raise


# --- start()/stop() lifecycle ----------------------------------------------------


async def test_start_is_idempotent() -> None:
    worker = ScheduledPromptWorker(InMemoryScheduledPromptRepository(), FakeService(), tick_interval_s=1000)
    await worker.start()
    task = worker._task
    await worker.start()

    assert worker._task is task
    await worker.stop()


async def test_stop_before_start_is_a_no_op() -> None:
    worker = ScheduledPromptWorker(InMemoryScheduledPromptRepository(), FakeService())
    await worker.stop()  # must not raise


async def test_stop_cancels_the_loop() -> None:
    worker = ScheduledPromptWorker(InMemoryScheduledPromptRepository(), FakeService(), tick_interval_s=1000)
    await worker.start()

    await worker.stop()

    assert worker._task is None


class RaisingRepository:
    """A repository whose `list_all()` always raises — simulates a failure in
    the tick machinery itself (e.g. a transient SQLite error), as opposed to
    `RaisingService`, which simulates one schedule's own run failing."""

    def __init__(self) -> None:
        self.list_all_calls = 0

    async def list_all(self):
        self.list_all_calls += 1
        raise RuntimeError("repository is unavailable")


async def test_loop_survives_tick_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `_loop` previously had no guard around `await self.tick()`
    itself — only `tick()`'s own per-entry try/excepts existed. A failure in
    the tick machinery before those (e.g. `list_all()` raising) would kill
    this `asyncio.Task` permanently, since nothing awaits it outside `stop()`
    and nothing would ever notice or retry. Proven here by ticking fast
    enough to observe more than one call despite every call raising, and by
    `stop()` not re-raising the stored exception of an already-dead task."""
    repo = RaisingRepository()
    worker = ScheduledPromptWorker(repo, FakeService(), tick_interval_s=0.01)

    await worker.start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if repo.list_all_calls >= 2:
            break
    await worker.stop()  # must not raise the RuntimeError stashed in a dead task

    assert repo.list_all_calls >= 2


class _FixedDatetime:
    """A drop-in replacement for the `datetime` class exposing only what
    `_tick`'s `datetime.now(...)` call needs, pinned to one instant regardless
    of the timezone passed in — the wall clock the test controls."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz):
        return self._fixed.astimezone(tz)
