"""Posted-comment delete-link storage.

Mirrors `app/services/scheduled_prompt_repository.py`'s shape: same in-memory/
SQLite split, same reasoning (in-memory is real but loses state on restart).
Simpler than that store — no `list_all`, since nothing ever needs to
enumerate every posted comment, only resolve one token at a time.
"""

from app.models.posted_comment import PostedComment


class InMemoryPostedCommentRepository:
    """Process-local posted-comment store."""

    def __init__(self) -> None:
        self._records: dict[str, PostedComment] = {}

    async def get(self, token: str) -> PostedComment | None:
        return self._records.get(token)

    async def save(self, record: PostedComment) -> PostedComment:
        self._records[record.id] = record
        return record

    async def delete(self, token: str) -> None:
        self._records.pop(token, None)
