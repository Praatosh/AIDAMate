"""Rendering the Scheduled Prompts dashboard for humans. See CLAUDE.md §1d.

Pure formatting — no I/O — mirroring `core/report.py`'s style. Linear renders
markdown tables natively, so this is the closest thing to a "visual" view of
every schedule that a plain Linear issue description can offer.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.scheduled_prompt import ScheduledPrompt

#: Title of the one dashboard issue per organization. Never searched for by
#: this text — the issue is found via a persisted `ScheduledPromptDashboard`
#: mapping — so this only needs to be recognizable to a human, not unique.
DASHBOARD_ISSUE_TITLE = "AIDA-MATE Scheduled Prompts"

_TABLE_HEADER = "| Title | Repository | Schedule | Enabled | Last run | |\n|---|---|---|---|---|---|"

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # datetime.weekday(): 0=Monday


def _ordinal(n: int) -> str:
    """`1` -> "1st", `2` -> "2nd", `11` -> "11th", etc."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _render_schedule(scheduled: ScheduledPrompt) -> str:
    """Render the 'Schedule' column, worded per `frequency`."""
    if scheduled.frequency == "once":
        return f"once {scheduled.run_on_date} {scheduled.run_at_time} {scheduled.timezone}"
    if scheduled.frequency == "hourly":
        return f"every {scheduled.interval_hours}h"
    if scheduled.frequency == "weekly":
        # `day_of_week` is bounds-checked (0-6) at creation only when
        # frequency == "weekly" (`_validate_frequency_fields` in
        # app/api/scheduled_prompts.py) — PATCH deliberately does not
        # cross-validate (CLAUDE.md §1d's documented gap), so a value left
        # out of range by an update is reachable here. An IndexError from a
        # single malformed row would otherwise abort the whole
        # organization's dashboard sync, not just that row.
        in_range = scheduled.day_of_week is not None and 0 <= scheduled.day_of_week <= 6
        day = _WEEKDAY_NAMES[scheduled.day_of_week] if in_range else "?"
        return f"{day} {scheduled.run_at_time} {scheduled.timezone} (weekly)"
    if scheduled.frequency == "monthly":
        day = _ordinal(scheduled.day_of_month) if scheduled.day_of_month is not None else "?"
        return f"{day} {scheduled.run_at_time} {scheduled.timezone} (monthly)"
    return f"{scheduled.run_at_time} {scheduled.timezone} (daily)"


def _render_last_run(scheduled: ScheduledPrompt) -> str:
    """Render the 'Last run' column. A real timestamp, not just a date — an
    hourly schedule needs more precision than "today" to be useful.

    `last_run_at` is stored in UTC (`ScheduledPrompt.mark_run`), but always
    localized into the schedule's own `timezone` for display here — the
    same zone the 'Schedule' column already uses, so a human never has to
    mentally convert UTC to make sense of when something last ran.
    `timezone` is validated at both creation and update (`_validate_timezone`
    in `app/api/scheduled_prompts.py`, applied by `ScheduledPromptUpdate` too),
    so `ZoneInfo(scheduled.timezone)` should never fail in practice — the
    `except` below is defensive insurance against that assumption ever
    silently stopping being true (a future write path, a hand-edited row),
    matching the `day_of_week` guard above: one malformed schedule must never
    abort the whole organization's dashboard sync.
    """
    if scheduled.last_run_at is None:
        return "never"
    try:
        local = scheduled.last_run_at.astimezone(ZoneInfo(scheduled.timezone))
        return f"{local.strftime('%Y-%m-%d %H:%M')} {scheduled.timezone}"
    except (ZoneInfoNotFoundError, ValueError):
        return f"{scheduled.last_run_at.isoformat()} (invalid timezone {scheduled.timezone!r})"


def _escape_cell(text: str) -> str:
    """Make freeform text safe to embed in a markdown table cell.

    `title` is the only fully-freeform field rendered here — every other
    column is already constrained by `app/api/scheduled_prompts.py`'s
    validators. A literal `|` would close the cell early and a newline would
    break the row onto a new markdown line, both corrupting the table.
    """
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _repository_link(repository: str) -> str:
    """Render `owner/repo` as a markdown link to its GitHub page.

    `repository` is already constrained to `owner/repo` by
    `app/api/scheduled_prompts.py`'s validator before a `ScheduledPrompt`
    can exist at all, so no further escaping is needed here.
    """
    return f"[{repository}](https://github.com/{repository})"


def _repository_cell(scheduled: ScheduledPrompt) -> str:
    """The 'Repository' column: the repo link, plus which PR it targets
    when `pr_number` is set (see `ScheduledPromptService.run`, CLAUDE.md
    §1d) — a schedule pointed at a PR studies that PR's head commit, not
    the repository's default branch, so the dashboard should say so."""
    link = _repository_link(scheduled.repository)
    if scheduled.pr_number is not None:
        return f"{link} PR #{scheduled.pr_number}"
    return link


def render_dashboard_description(schedules: list[ScheduledPrompt], *, base_url: str) -> str:
    """Render the full dashboard issue description for one organization's schedules.

    `base_url` (`settings.public_base_url`) builds both the "create a new
    schedule" link and each row's "Delete" link — rendered fresh from it on
    every sync rather than written once at creation time, so neither can go
    stale if `PUBLIC_BASE_URL` changes later. A row's delete link opens a
    confirmation page (`app/api/scheduled_prompt_form.py`'s
    `GET /scheduled-prompts/{id}/delete`) with a real button, never deletes
    directly from a bare link — the same GET-shows/POST-acts discipline
    `merge_confirmation.py` already uses, so a link preview or crawler
    fetching the URL can't trigger a deletion.
    """
    link_line = f"Create a new scheduled prompt: {base_url}/scheduled-prompts/new"

    if not schedules:
        return f"{DASHBOARD_ISSUE_TITLE}\n\n{link_line}\n\nNo scheduled prompts configured."

    rows = [
        "| {title} | {repository} | {schedule} | {enabled} | {last_run} | {delete} |".format(
            title=_escape_cell(scheduled.title),
            repository=_repository_cell(scheduled),
            schedule=_render_schedule(scheduled),
            enabled="✅" if scheduled.enabled else "⏸️",
            last_run=_render_last_run(scheduled),
            delete=f"[Delete]({base_url}/scheduled-prompts/{scheduled.id}/delete)",
        )
        for scheduled in sorted(schedules, key=lambda s: s.title.lower())
    ]
    return "\n".join([link_line, "", _TABLE_HEADER, *rows])
