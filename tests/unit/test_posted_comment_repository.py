"""Posted-comment delete-link storage: both InMemory and SQLite implementations
against the same `IPostedCommentRepository` contract (get/save/delete).
"""

from pathlib import Path

import pytest

from app.models.posted_comment import PostedComment
from app.services.posted_comment_repository import InMemoryPostedCommentRepository
from app.services.sqlite_posted_comment_repository import SqlitePostedCommentRepository


def _record(**overrides) -> PostedComment:
    values = {"id": "token-1", "linear_comment_id": "comment-1", "organization_id": "org-1"}
    values.update(overrides)
    return PostedComment(**values)


@pytest.fixture(params=["memory", "sqlite"])
def repo(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "memory":
        return InMemoryPostedCommentRepository()
    return SqlitePostedCommentRepository(tmp_path / "posted_comments.sqlite3")


async def test_get_on_an_unknown_token_returns_none(repo) -> None:
    assert await repo.get("nope") is None


async def test_save_then_get(repo) -> None:
    await repo.save(_record())

    found = await repo.get("token-1")

    assert found is not None
    assert found.linear_comment_id == "comment-1"
    assert found.organization_id == "org-1"


async def test_save_without_an_organization_id(repo) -> None:
    await repo.save(_record(id="token-2", organization_id=None))

    found = await repo.get("token-2")

    assert found is not None
    assert found.organization_id is None


async def test_delete_removes_the_record(repo) -> None:
    await repo.save(_record())

    await repo.delete("token-1")

    assert await repo.get("token-1") is None


async def test_delete_on_an_unknown_token_does_not_raise(repo) -> None:
    await repo.delete("nope")  # must not raise
