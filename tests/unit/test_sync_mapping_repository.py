"""In-memory sync-mapping repository (CLAUDE.md §1c)."""

import asyncio

from app.models.sync_mapping import SyncMapping
from app.services.sync_mapping_repository import InMemorySyncMappingRepository


def _mapping(fingerprint: str = "github:acme/api:issue:42") -> SyncMapping:
    return SyncMapping(
        fingerprint=fingerprint,
        source_type="issue",
        source_id="42",
        repository="acme/api",
        github_url="https://github.com/acme/api/issues/42",
        state="open",
    )


async def test_create_then_find_by_fingerprint() -> None:
    repo = InMemorySyncMappingRepository()
    mapping, created = await repo.create(_mapping())

    assert created is True
    found = await repo.find_by_fingerprint(mapping.fingerprint)
    assert found is mapping


async def test_find_by_unknown_fingerprint_returns_none() -> None:
    assert await InMemorySyncMappingRepository().find_by_fingerprint("nope") is None


async def test_duplicate_fingerprint_returns_the_existing_mapping() -> None:
    """The dedup guarantee: a second delivery for the same GitHub object must
    not create a second mapping."""
    repo = InMemorySyncMappingRepository()
    first, created_first = await repo.create(_mapping())
    second, created_second = await repo.create(_mapping())

    assert created_first is True
    assert created_second is False
    assert second.id == first.id


async def test_distinct_fingerprints_create_distinct_mappings() -> None:
    repo = InMemorySyncMappingRepository()
    await repo.create(_mapping("github:acme/api:issue:42"))
    await repo.create(_mapping("github:acme/api:issue:43"))

    assert await repo.find_by_fingerprint("github:acme/api:issue:42") is not None
    assert await repo.find_by_fingerprint("github:acme/api:issue:43") is not None


async def test_concurrent_creates_with_same_fingerprint_yield_one_mapping() -> None:
    repo = InMemorySyncMappingRepository()

    results = await asyncio.gather(*(repo.create(_mapping()) for _ in range(10)))

    assert len({mapping.id for mapping, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_save_persists_mutations() -> None:
    repo = InMemorySyncMappingRepository()
    mapping, _ = await repo.create(_mapping())

    mapping.linear_issue_id = "issue-1"
    mapping.state = "closed"
    await repo.save(mapping)

    found = await repo.find_by_fingerprint(mapping.fingerprint)
    assert found.linear_issue_id == "issue-1"
    assert found.state == "closed"


async def test_find_by_linear_issue_id() -> None:
    repo = InMemorySyncMappingRepository()
    mapping, _ = await repo.create(_mapping())
    mapping.linear_issue_id = "issue-1"
    await repo.save(mapping)

    found = await repo.find_by_linear_issue_id("issue-1")
    assert found is not None
    assert found.id == mapping.id


async def test_find_by_unknown_linear_issue_id_returns_none() -> None:
    assert await InMemorySyncMappingRepository().find_by_linear_issue_id("nope") is None
