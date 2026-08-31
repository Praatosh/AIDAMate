"""Durable GitHub-object <-> Linear-issue mapping storage. See CLAUDE.md §1c.

Satisfies the same `ISyncMappingRepository` contract as
`InMemorySyncMappingRepository`. Shares its underlying SQLite file with
`SqliteReviewJobRepository` (both are constructed against
`settings.review_store_path`) but owns a completely separate table — one
file, two independent schemas, exactly as SQLite supports natively; there is
no relationship between a `sync_mappings` row and a `review_jobs` row.

The uniqueness index here is a **full** unique index on `fingerprint`, unlike
`review_jobs`' partial one. A `SyncMapping` has no "terminal vs. active"
lifecycle the way a `ReviewJob` does — it should simply never duplicate,
ever, for as long as it exists.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.models.sync_mapping import SyncMapping

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_mappings (
    id             TEXT PRIMARY KEY,
    fingerprint    TEXT NOT NULL,
    source_type    TEXT NOT NULL,
    repository     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_mappings_fingerprint ON sync_mappings (fingerprint);
"""


class SqliteSyncMappingRepository:
    """Sync-mapping store persisted to a local SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection, guaranteed closed on exit.

        Same per-operation reasoning as `SqliteReviewJobRepository._connect`:
        `asyncio.to_thread` may run any given call on any worker thread. Also
        see that same docstring for why this is a context manager rather than
        a plain method returning the raw connection — `sqlite3.Connection`'s
        own context manager only commits/rolls back, never closes.
        """
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _insert_sync(self, mapping: SyncMapping) -> tuple[SyncMapping, bool]:
        """Insert `mapping`, or return the existing row for the same fingerprint.

        Returns `(mapping, created)`. Atomicity comes from the UNIQUE index,
        mirroring `SqliteReviewJobRepository._insert_sync`: on
        `IntegrityError`, some concurrent caller already won — read back and
        return their row rather than raising.
        """
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO sync_mappings (id, fingerprint, source_type, repository, "
                    "created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        mapping.id,
                        mapping.fingerprint,
                        mapping.source_type,
                        mapping.repository,
                        mapping.created_at.isoformat(),
                        mapping.model_dump_json(),
                    ),
                )
                return mapping, True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT payload FROM sync_mappings WHERE fingerprint = ?",
                    (mapping.fingerprint,),
                ).fetchone()

        if row is None:  # pragma: no cover - only reachable if the row vanished mid-race
            return mapping, True
        return SyncMapping.model_validate_json(row["payload"]), False

    async def create(self, mapping: SyncMapping) -> tuple[SyncMapping, bool]:
        """Store a new mapping, or return the existing one for the same fingerprint."""
        return await asyncio.to_thread(self._insert_sync, mapping)

    async def save(self, mapping: SyncMapping) -> SyncMapping:
        """Persist mutations to an existing mapping."""

        def _save() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE sync_mappings SET payload = ? WHERE id = ?",
                    (mapping.model_dump_json(), mapping.id),
                )

        await asyncio.to_thread(_save)
        return mapping

    async def find_by_fingerprint(self, fingerprint: str) -> SyncMapping | None:
        """Look up an existing mapping by its dedup fingerprint, if any."""

        def _query() -> sqlite3.Row | None:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM sync_mappings WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()

        row = await asyncio.to_thread(_query)
        return SyncMapping.model_validate_json(row["payload"]) if row else None

    async def find_by_linear_issue_id(self, linear_issue_id: str) -> SyncMapping | None:
        """Look up the mapping that created/updates a given Linear issue, if any.

        `linear_issue_id` isn't an indexed column (it lives inside the JSON
        `payload` blob), so this scans and filters in Python — same tradeoff,
        same justification as `SqliteReviewJobRepository.
        find_latest_completed_by_linear_issue_id`. Fine at this project's
        scale; a dedicated indexed column would be the fix if that changes.
        """

        def _query() -> list[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute("SELECT payload FROM sync_mappings").fetchall()

        rows = await asyncio.to_thread(_query)
        for row in rows:
            mapping = SyncMapping.model_validate_json(row["payload"])
            if mapping.linear_issue_id == linear_issue_id:
                return mapping
        return None
