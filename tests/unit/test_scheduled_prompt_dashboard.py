"""The scheduled-prompts dashboard's pure render function (CLAUDE.md §1d)."""

from datetime import UTC, datetime

from app.core.scheduled_prompt_dashboard import DASHBOARD_ISSUE_TITLE, render_dashboard_description
from app.models.scheduled_prompt import ScheduledPrompt

_BASE_URL = "http://localhost:8000"


def _scheduled(**overrides) -> ScheduledPrompt:
    values = {
        "title": "Security audit",
        "prompt": "Run a general security audit of this repository.",
        "repository": "acme/api",
        "frequency": "daily",
        "run_at_time": "09:00",
        "timezone": "Asia/Kolkata",
        "linear_issue_id": "issue-1",
        "organization_id": "org-1",
    }
    values.update(overrides)
    return ScheduledPrompt(**values)


def _render(schedules: list[ScheduledPrompt]) -> str:
    return render_dashboard_description(schedules, base_url=_BASE_URL)


def test_empty_list_renders_a_no_schedules_message() -> None:
    text = _render([])

    assert "No scheduled prompts configured" in text
    assert "|" not in text  # no table at all, not an empty one


def test_link_to_the_creation_form_is_present_in_both_states() -> None:
    form_url = f"{_BASE_URL}/scheduled-prompts/new"
    assert form_url in _render([])
    assert form_url in _render([_scheduled()])


def test_renders_a_markdown_table_header() -> None:
    text = _render([_scheduled()])

    assert "| Title | Repository | Schedule | Enabled | Last run | |" in text
    assert "|---|---|---|---|---|---|" in text


def test_renders_one_row_per_schedule() -> None:
    text = _render([_scheduled(title="First"), _scheduled(title="Second")])

    assert "First" in text
    assert "Second" in text
    rows = text.splitlines()[4:]
    assert len(rows) == 2


def test_row_includes_repository_schedule_and_last_run() -> None:
    scheduled = _scheduled(
        repository="acme/api",
        run_at_time="09:00",
        timezone="Asia/Kolkata",
        last_run_at=datetime(2026, 8, 24, 3, 30, tzinfo=UTC),
    )
    text = _render([scheduled])

    assert "acme/api" in text
    assert "09:00 Asia/Kolkata (daily)" in text
    # Localized into the schedule's own timezone, not left as UTC: 03:30 UTC -> 09:00 IST.
    assert "2026-08-24 09:00 Asia/Kolkata" in text


def test_never_run_shows_never() -> None:
    text = _render([_scheduled(last_run_at=None)])

    assert "never" in text


def test_schedule_column_per_frequency() -> None:
    assert "once 2026-08-25 09:00 Asia/Kolkata" in _render(
        [_scheduled(frequency="once", run_on_date="2026-08-25", run_at_time="09:00")]
    )
    assert "every 3h" in _render(
        [_scheduled(frequency="hourly", interval_hours=3, run_at_time=None)]
    )
    assert "Wed 09:00 Asia/Kolkata (weekly)" in _render(
        [_scheduled(frequency="weekly", day_of_week=2, run_at_time="09:00")]
    )
    assert "1st 09:00 Asia/Kolkata (monthly)" in _render(
        [_scheduled(frequency="monthly", day_of_month=1, run_at_time="09:00")]
    )


def test_repository_renders_as_a_link_to_github() -> None:
    text = _render([_scheduled(repository="acme/api")])

    assert "[acme/api](https://github.com/acme/api)" in text


def test_repository_cell_shows_the_targeted_pr_number_when_set() -> None:
    text = _render([_scheduled(repository="acme/api", pr_number=123)])

    assert "[acme/api](https://github.com/acme/api) PR #123" in text


def test_repository_cell_omits_pr_number_when_not_set() -> None:
    text = _render([_scheduled(repository="acme/api")])

    assert "PR #" not in text


def test_row_includes_a_delete_link_for_that_schedule() -> None:
    scheduled = _scheduled()
    text = _render([scheduled])

    assert f"[Delete]({_BASE_URL}/scheduled-prompts/{scheduled.id}/delete)" in text


def test_enabled_and_disabled_render_distinct_markers() -> None:
    enabled_text = _render([_scheduled(enabled=True)])
    disabled_text = _render([_scheduled(enabled=False)])

    enabled_row = enabled_text.splitlines()[-1]
    disabled_row = disabled_text.splitlines()[-1]
    assert enabled_row != disabled_row


def test_title_containing_a_pipe_does_not_corrupt_the_table() -> None:
    text = _render([_scheduled(title="Check | this")])

    rows = text.splitlines()[4:]
    assert len(rows) == 1
    assert rows[0].count("|") == 8  # 7 column delimiters (6 columns) + the escaped one inside the title


def test_title_containing_a_newline_stays_on_one_row() -> None:
    text = _render([_scheduled(title="Multi\nline")])

    rows = text.splitlines()[4:]
    assert len(rows) == 1
    assert "Multi line" in rows[0]


def test_rows_are_sorted_by_title() -> None:
    text = _render([_scheduled(title="Zebra"), _scheduled(title="Alpha")])

    rows = text.splitlines()[4:]
    assert rows[0].startswith("| Alpha")
    assert rows[1].startswith("| Zebra")


def test_dashboard_title_constant_is_a_recognizable_name() -> None:
    assert "AIDA-MATE" in DASHBOARD_ISSUE_TITLE
    assert "Scheduled Prompts" in DASHBOARD_ISSUE_TITLE
