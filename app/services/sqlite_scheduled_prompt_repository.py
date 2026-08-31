"""Durable scheduled-prompt storage. See CLAUDE.md §1d.

Satisfies the same `IScheduledPromptRepository` contract as
`InMemoryScheduledPromptRepository`. Shares its underlying SQLite file with
`SqliteReviewJobRepository`/`SqliteSyncMappingRepository` (all three are
constructed against `settings.review_store_path`) but owns a completely
separate table.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.models.scheduled_prompt import ScheduledPrompt

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_prompts (
    id             TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);
"""


class SqliteScheduledPromptRepository:
    """Scheduled-prompt store persisted to a local SQLite file."""

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

    async def create(self, scheduled: ScheduledPrompt) -> ScheduledPrompt:
        def _insert() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO scheduled_prompts (id, created_at, payload) VALUES (?, ?, ?)",
                    (scheduled.id, scheduled.created_at.isoformat(), scheduled.model_dump_json()),
                )

        await asyncio.to_thread(_insert)
        return scheduled

    async def save(self, scheduled: ScheduledPrompt) -> ScheduledPrompt:
        """Persist mutations to an existing schedule."""

        def _save() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE scheduled_prompts SET payload = ? WHERE id = ?",
                    (scheduled.model_dump_json(), scheduled.id),
                )

        await asyncio.to_thread(_save)
        return scheduled

    async def get(self, scheduled_id: str) -> ScheduledPrompt | None:
        def _query() -> sqlite3.Row | None:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM scheduled_prompts WHERE id = ?", (scheduled_id,)
                ).fetchone()

        row = await asyncio.to_thread(_query)
        return ScheduledPrompt.model_validate_json(row["payload"]) if row else None

    async def list_all(self) -> list[ScheduledPrompt]:
        def _query() -> list[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute("SELECT payload FROM scheduled_prompts").fetchall()

        rows = await asyncio.to_thread(_query)
        return [ScheduledPrompt.model_validate_json(row["payload"]) for row in rows]

    async def delete(self, scheduled_id: str) -> None:
        def _delete() -> None:
            with self._connect() as conn:
                conn.execute("DELETE FROM scheduled_prompts WHERE id = ?", (scheduled_id,))

        await asyncio.to_thread(_delete)
