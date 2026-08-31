"""Durable scheduled-prompt storage (CLAUDE.md §1d).

Runs against a real SQLite file in `tmp_path`, same reasoning as
`test_sqlite_sync_mapping_repository.py`: the property worth proving (state
surviving a new instance) only exists in the database itself.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.scheduled_prompt import ScheduledPrompt
from app.services.sqlite_scheduled_prompt_repository import SqliteScheduledPromptRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "scheduled.sqlite3"


@pytest.fixture
def repo(db_path: Path) -> SqliteScheduledPromptRepository:
    return SqliteScheduledPromptRepository(db_path)


def _scheduled(title: str = "Security audit", **overrides) -> ScheduledPrompt:
    values = {
        "title": title,
        "prompt": "Run a general security audit of this repository.",
        "repository": "acme/api",
        "run_at_time": "09:00",
        "timezone": "Asia/Kolkata",
        "linear_issue_id": "issue-1",
    }
    values.update(overrides)
    return ScheduledPrompt(**values)


async def test_create_then_get(repo: SqliteScheduledPromptRepository) -> None:
    created = await repo.create(_scheduled())

    found = await repo.get(created.id)
    assert found is not None
    assert found.id == created.id
    assert found.title == "Security audit"


async def test_get_unknown_id_returns_none(repo: SqliteScheduledPromptRepository) -> None:
    assert await repo.get("nope") is None


async def test_list_all_returns_every_schedule(repo: SqliteScheduledPromptRepository) -> None:
    await repo.create(_scheduled("First"))
    await repo.create(_scheduled("Second"))

    titles = {scheduled.title for scheduled in await repo.list_all()}
    assert titles == {"First", "Second"}


async def test_save_persists_mutations(repo: SqliteScheduledPromptRepository) -> None:
    scheduled = await repo.create(_scheduled())

    scheduled.enabled = False
    scheduled.last_run_at = datetime(2026, 8, 24, 3, 30, tzinfo=UTC)
    await repo.save(scheduled)

    found = await repo.get(scheduled.id)
    assert found.enabled is False
    assert found.last_run_at == datetime(2026, 8, 24, 3, 30, tzinfo=UTC)


async def test_delete_removes_the_schedule(repo: SqliteScheduledPromptRepository) -> None:
    scheduled = await repo.create(_scheduled())

    await repo.delete(scheduled.id)

    assert await repo.get(scheduled.id) is None


async def test_state_survives_a_new_repository_instance(db_path: Path) -> None:
    """The scenario this class exists for: a server restart."""
    created = await SqliteScheduledPromptRepository(db_path).create(_scheduled())

    reopened = SqliteScheduledPromptRepository(db_path)

    found = await reopened.get(created.id)
    assert found is not None
    assert found.id == created.id
