"""Durable scheduled-prompts-dashboard storage, keyed by (organization, team).

Satisfies the same `IScheduledPromptDashboardRepository` contract as
`InMemoryScheduledPromptDashboardRepository`. Shares its underlying SQLite
file with the other repository pairs here (all constructed against
`settings.review_store_path`) but owns a completely separate table.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_prompt_dashboards (
    organization_id TEXT NOT NULL,
    team_id         TEXT NOT NULL,
    payload         TEXT NOT NULL,
    PRIMARY KEY (organization_id, team_id)
);
"""

#: The table's key changed shape (organization_id alone -> composite
#: (organization_id, team_id)) when the dashboard fanned out to one issue
#: per team (CLAUDE.md §1d) — a plain ALTER TABLE ADD COLUMN can't express
#: that, since the old single-column PRIMARY KEY would still reject a
#: second row per organization. Renamed, not dropped, so old data isn't
#: silently destroyed; each org simply gets a fresh per-team row created on
#: its next sync, the same "no explicit migration, becomes eligible again"
#: posture the last_run_date -> last_run_at cutover used.
_LEGACY_TABLE_NAME = "scheduled_prompt_dashboards_legacy_pre_teams"


class SqliteScheduledPromptDashboardRepository:
    """Dashboard store persisted to a local SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(scheduled_prompt_dashboards)")
            }
            if columns and "team_id" not in columns:
                conn.execute(
                    f"ALTER TABLE scheduled_prompt_dashboards RENAME TO {_LEGACY_TABLE_NAME}"
                )
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

    async def get(self, organization_id: str, team_id: str) -> ScheduledPromptDashboard | None:
        def _query() -> sqlite3.Row | None:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM scheduled_prompt_dashboards "
                    "WHERE organization_id = ? AND team_id = ?",
                    (organization_id, team_id),
                ).fetchone()

        row = await asyncio.to_thread(_query)
        return ScheduledPromptDashboard.model_validate_json(row["payload"]) if row else None

    async def save(self, dashboard: ScheduledPromptDashboard) -> ScheduledPromptDashboard:
        """Insert or replace the dashboard for its (organization, team)."""

        def _save() -> None:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO scheduled_prompt_dashboards (organization_id, team_id, payload) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(organization_id, team_id) DO UPDATE SET payload = excluded.payload",
                    (dashboard.organization_id, dashboard.team_id, dashboard.model_dump_json()),
                )

        await asyncio.to_thread(_save)
        return dashboard
