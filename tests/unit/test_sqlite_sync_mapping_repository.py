"""Durable sync-mapping storage (CLAUDE.md §1c).

Runs against a real SQLite file in `tmp_path`, same reasoning as
`test_sqlite_job_repository.py`: the property worth proving (the UNIQUE
index settling a race) only exists in the database itself.
"""

import asyncio
from pathlib import Path

import pytest

from app.models.sync_mapping import SyncMapping
from app.services.sqlite_sync_mapping_repository import SqliteSyncMappingRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sync.sqlite3"


@pytest.fixture
def repo(db_path: Path) -> SqliteSyncMappingRepository:
    return SqliteSyncMappingRepository(db_path)


def _mapping(fingerprint: str = "github:acme/api:issue:42", **overrides) -> SyncMapping:
    values = {
        "fingerprint": fingerprint,
        "source_type": "issue",
        "source_id": "42",
        "repository": "acme/api",
        "github_url": "https://github.com/acme/api/issues/42",
        "state": "open",
    }
    values.update(overrides)
    return SyncMapping(**values)


async def test_create_then_find_by_fingerprint(repo: SqliteSyncMappingRepository) -> None:
    mapping, created = await repo.create(_mapping())

    assert created is True
    found = await repo.find_by_fingerprint(mapping.fingerprint)
    assert found is not None
    assert found.id == mapping.id


async def test_find_by_unknown_fingerprint_returns_none(repo: SqliteSyncMappingRepository) -> None:
    assert await repo.find_by_fingerprint("nope") is None


async def test_duplicate_fingerprint_returns_the_existing_mapping(
    repo: SqliteSyncMappingRepository,
) -> None:
    first, created_first = await repo.create(_mapping())
    second, created_second = await repo.create(_mapping())

    assert created_first is True
    assert created_second is False
    assert second.id == first.id


async def test_concurrent_creates_produce_exactly_one_mapping(repo: SqliteSyncMappingRepository) -> None:
    """The race the UNIQUE index exists to settle."""
    results = await asyncio.gather(*(repo.create(_mapping()) for _ in range(8)))

    created = [mapping for mapping, was_created in results if was_created]
    assert len(created) == 1
    assert len({mapping.id for mapping, _ in results}) == 1


async def test_save_persists_mutations(repo: SqliteSyncMappingRepository) -> None:
    mapping, _ = await repo.create(_mapping())

    mapping.linear_issue_id = "issue-1"
    mapping.state = "closed"
    await repo.save(mapping)

    found = await repo.find_by_fingerprint(mapping.fingerprint)
    assert found.linear_issue_id == "issue-1"
    assert found.state == "closed"


async def test_state_survives_a_new_repository_instance(db_path: Path) -> None:
    """The scenario this class exists for: a server restart."""
    mapping, _ = await SqliteSyncMappingRepository(db_path).create(_mapping())

    reopened = SqliteSyncMappingRepository(db_path)

    found = await reopened.find_by_fingerprint(mapping.fingerprint)
    assert found is not None
    assert found.id == mapping.id


async def test_find_by_linear_issue_id(repo: SqliteSyncMappingRepository) -> None:
    mapping, _ = await repo.create(_mapping())
    mapping.linear_issue_id = "issue-1"
    await repo.save(mapping)

    found = await repo.find_by_linear_issue_id("issue-1")
    assert found is not None
    assert found.id == mapping.id


async def test_find_by_unknown_linear_issue_id_returns_none(repo: SqliteSyncMappingRepository) -> None:
    assert await repo.find_by_linear_issue_id("nope") is None
