"""In-memory scheduled-prompt repository (CLAUDE.md §1d)."""

from datetime import UTC, datetime

from app.models.scheduled_prompt import ScheduledPrompt
from app.services.scheduled_prompt_repository import InMemoryScheduledPromptRepository


def _scheduled(title: str = "Security audit") -> ScheduledPrompt:
    return ScheduledPrompt(
        title=title,
        prompt="Run a general security audit of this repository.",
        repository="acme/api",
        run_at_time="09:00",
        timezone="Asia/Kolkata",
        linear_issue_id="issue-1",
    )


async def test_create_then_get() -> None:
    repo = InMemoryScheduledPromptRepository()
    created = await repo.create(_scheduled())

    found = await repo.get(created.id)
    assert found is created


async def test_get_unknown_id_returns_none() -> None:
    assert await InMemoryScheduledPromptRepository().get("nope") is None


async def test_list_all_returns_every_schedule() -> None:
    repo = InMemoryScheduledPromptRepository()
    await repo.create(_scheduled("First"))
    await repo.create(_scheduled("Second"))

    titles = {scheduled.title for scheduled in await repo.list_all()}
    assert titles == {"First", "Second"}


async def test_list_all_on_empty_repository_is_empty() -> None:
    assert await InMemoryScheduledPromptRepository().list_all() == []


async def test_save_persists_mutations() -> None:
    repo = InMemoryScheduledPromptRepository()
    scheduled = await repo.create(_scheduled())

    scheduled.enabled = False
    scheduled.last_run_at = datetime(2026, 8, 24, 3, 30, tzinfo=UTC)
    await repo.save(scheduled)

    found = await repo.get(scheduled.id)
    assert found.enabled is False
    assert found.last_run_at == datetime(2026, 8, 24, 3, 30, tzinfo=UTC)


async def test_delete_removes_the_schedule() -> None:
    repo = InMemoryScheduledPromptRepository()
    scheduled = await repo.create(_scheduled())

    await repo.delete(scheduled.id)

    assert await repo.get(scheduled.id) is None
    assert await repo.list_all() == []


async def test_delete_unknown_id_is_a_no_op() -> None:
    repo = InMemoryScheduledPromptRepository()
    await repo.delete("nope")  # must not raise
