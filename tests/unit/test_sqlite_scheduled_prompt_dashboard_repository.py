"""Durable scheduled-prompts-dashboard storage (CLAUDE.md §1d).

Runs against a real SQLite file in `tmp_path`, same reasoning as the other
SQLite repository tests here: the property worth proving (upsert, state
surviving a new instance, and the legacy-schema cutover) only exists in the
database itself.
"""

import sqlite3
from pathlib import Path

import pytest

from app.models.scheduled_prompt_dashboard import ScheduledPromptDashboard
from app.services.sqlite_scheduled_prompt_dashboard_repository import (
    _LEGACY_TABLE_NAME,
    SqliteScheduledPromptDashboardRepository,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "dashboard.sqlite3"


@pytest.fixture
def repo(db_path: Path) -> SqliteScheduledPromptDashboardRepository:
    return SqliteScheduledPromptDashboardRepository(db_path)


def _dashboard(
    organization_id: str = "org-1", team_id: str = "team-1", linear_issue_id: str = "issue-dash-1"
) -> ScheduledPromptDashboard:
    return ScheduledPromptDashboard(
        organization_id=organization_id, team_id=team_id, linear_issue_id=linear_issue_id
    )


async def test_get_unknown_organization_returns_none(repo: SqliteScheduledPromptDashboardRepository) -> None:
    assert await repo.get("nope", "team-1") is None


async def test_save_then_get(repo: SqliteScheduledPromptDashboardRepository) -> None:
    await repo.save(_dashboard())

    found = await repo.get("org-1", "team-1")
    assert found is not None
    assert found.linear_issue_id == "issue-dash-1"


async def test_save_upserts_by_organization_and_team_id(
    repo: SqliteScheduledPromptDashboardRepository,
) -> None:
    await repo.save(_dashboard(linear_issue_id="issue-old"))
    await repo.save(_dashboard(linear_issue_id="issue-new"))

    found = await repo.get("org-1", "team-1")
    assert found.linear_issue_id == "issue-new"


async def test_distinct_teams_in_the_same_organization_get_distinct_dashboards(
    repo: SqliteScheduledPromptDashboardRepository,
) -> None:
    await repo.save(_dashboard("org-1", "team-1", "issue-1"))
    await repo.save(_dashboard("org-1", "team-2", "issue-2"))

    assert (await repo.get("org-1", "team-1")).linear_issue_id == "issue-1"
    assert (await repo.get("org-1", "team-2")).linear_issue_id == "issue-2"


async def test_state_survives_a_new_repository_instance(db_path: Path) -> None:
    """The scenario this class exists for: a server restart."""
    await SqliteScheduledPromptDashboardRepository(db_path).save(_dashboard())

    reopened = SqliteScheduledPromptDashboardRepository(db_path)

    found = await reopened.get("org-1", "team-1")
    assert found is not None
    assert found.linear_issue_id == "issue-dash-1"


def test_old_schema_table_is_renamed_not_dropped(db_path: Path) -> None:
    """A pre-teamspace-fanout DB has `organization_id` as the sole primary
    key with no `team_id` column. The repository must migrate it out of the
    way rather than crash or silently lose the old rows."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE scheduled_prompt_dashboards (organization_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO scheduled_prompt_dashboards VALUES (?, ?)",
        ("org-1", '{"organization_id": "org-1", "linear_issue_id": "issue-old"}'),
    )
    conn.commit()
    conn.close()

    SqliteScheduledPromptDashboardRepository(db_path)

    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert _LEGACY_TABLE_NAME in tables
    legacy_row = conn.execute(f"SELECT organization_id FROM {_LEGACY_TABLE_NAME}").fetchone()
    assert legacy_row == ("org-1",)  # old data preserved, not deleted

    columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_prompt_dashboards)")}
    assert "team_id" in columns
    conn.close()
