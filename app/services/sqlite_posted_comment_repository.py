"""Durable posted-comment delete-link storage.

Satisfies the same `IPostedCommentRepository` contract as
`InMemoryPostedCommentRepository`. Shares its underlying SQLite file with
`SqliteReviewJobRepository`/`SqliteSyncMappingRepository`/
`SqliteScheduledPromptRepository` (all constructed against
`settings.review_store_path`) but owns a completely separate table.

`save` is `INSERT OR REPLACE` rather than a plain `UPDATE`, unlike
`SqliteScheduledPromptRepository.save` — there's no separate `create()` in
this store's contract (`IPostedCommentRepository` has only `get`/`save`/
`delete`), so `save` is always the first write for a given token too.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.models.posted_comment import PostedComment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posted_comments (
    id             TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);
"""


class SqlitePostedCommentRepository:
    """Posted-comment store persisted to a local SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection, guaranteed closed on exit.

        See `SqliteReviewJobRepository._connect` for why this is a context
        manager rather than a plain method returning the raw connection —
        `sqlite3.Connection`'s own context manager only commits/rolls back,
        never closes.
        """
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    async def get(self, token: str) -> PostedComment | None:
        def _query() -> sqlite3.Row | None:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM posted_comments WHERE id = ?", (token,)
                ).fetchone()

        row = await asyncio.to_thread(_query)
        return PostedComment.model_validate_json(row["payload"]) if row else None

    async def save(self, record: PostedComment) -> PostedComment:
        def _save() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO posted_comments (id, created_at, payload) VALUES (?, ?, ?)",
                    (record.id, record.created_at.isoformat(), record.model_dump_json()),
                )

        await asyncio.to_thread(_save)
        return record

    async def delete(self, token: str) -> None:
        def _delete() -> None:
            with self._connect() as conn:
                conn.execute("DELETE FROM posted_comments WHERE id = ?", (token,))

        await asyncio.to_thread(_delete)
