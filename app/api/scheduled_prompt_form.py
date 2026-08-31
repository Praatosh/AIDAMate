"""A simple HTML form for creating a scheduled prompt. See CLAUDE.md §1d.

A second HTML page in the app, alongside `merge_confirmation.py` — kept in
its own module for the same reason that one is: this codebase deliberately
separates JSON API routes from HTML pages, never mixing the two in one
router. Reuses `scheduled_prompts.py`'s `ScheduledPromptCreate` and
`create_scheduled_prompt` directly rather than reimplementing validation,
organization resolution, or dashboard sync — this is a second *entry point*
into that exact same creation path, not a parallel one.

Four simplifications from the JSON API, all explicit user choices:

* No timezone selector — every prompt created here runs on `Asia/Kolkata`
  (IST). The JSON API still accepts any IANA timezone for callers that need
  one; this form exists specifically for the IST case.
* No target-issue field — every submission's result posts to the
  organization's dashboard issue itself
  (`ScheduledPromptDashboardService.ensure`), rather than a separately
  chosen issue. Config and results both live in one place.
* The repository field takes a GitHub URL (e.g. the address-bar URL of the
  repo you're already looking at), not the `owner/repo` slug
  `ScheduledPrompt.repository` actually stores — `_parse_github_repository`
  extracts it, so there's no manual reformatting to get right.
* The frequency-specific fields (`run_on_date`/`interval_hours`/
  `day_of_week`/`day_of_month`/`run_at_time`) are all plain optional
  strings at the FastAPI parameter level, not typed `int | None = Form(...)`
  — a browser still submits a `display:none`-hidden field's value (empty
  string) unless it's `disabled`, so typed int coercion would 422 on
  whichever fields the chosen frequency doesn't use. `_to_int` converts by
  hand instead, treating blank as "not provided" and reporting a clear
  form error rather than a raw 422 for a genuinely non-numeric value.
"""

import html
import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app.api.scheduled_prompts import (
    ScheduledPromptCreate,
    _resolve_organization_id,
    create_scheduled_prompt,
    delete_scheduled_prompt,
)
from app.core.logging import get_logger
from app.core.scheduled_prompt_dashboard import _render_schedule
from app.models.scheduled_prompt import ScheduledPrompt

logger = get_logger(__name__)

router = APIRouter(prefix="/scheduled-prompts", tags=["scheduled-prompts-form"])

_IST = "Asia/Kolkata"
_WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

#: Matches a GitHub repo URL with or without scheme/`www.`, and tolerates a
#: trailing `.git`, slash, or further path/query/fragment (e.g. `/tree/main`)
#: — whatever shape a browser's address bar or `git clone` happens to hand
#: back. Owner and repo are each a single non-slash path segment.
_GITHUB_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s?#]+)/(?P<repo>[^/\s?#]+?)"
    r"(?:\.git)?(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _parse_github_repository(value: str) -> str | None:
    """Extract `owner/repo` from a pasted GitHub URL, or None if it doesn't look like one."""
    match = _GITHUB_URL_PATTERN.match(value.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


#: Matches a GitHub pull request URL specifically (`.../pull/123` or
#: `.../pulls/123`), tolerating the same trailing path/query/fragment
#: `_GITHUB_URL_PATTERN` does (e.g. `/files`, `#discussion_r1`).
_GITHUB_PR_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/[^/\s?#]+/[^/\s?#]+/pulls?/(?P<number>\d+)"
    r"(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _parse_pr_number(value: str) -> int | None:
    """Extract a PR number from a pasted GitHub PR URL, or None if the link
    doesn't point at a specific PR (e.g. a plain repo URL) — lets the same
    repository-link field target either the whole repo or one specific PR's
    head commit, taking precedence over `branch`/the default branch when set."""
    match = _GITHUB_PR_URL_PATTERN.match(value.strip())
    return int(match.group("number")) if match else None


def _to_int(value: str) -> int | None:
    """Blank -> None (field not relevant to the chosen frequency); otherwise parse as int."""
    stripped = value.strip()
    return int(stripped) if stripped else None


#: Shared styling for every page this module renders (form, success,
#: confirm-delete, deleted, not-found). Inline `<style>`, not a separate
#: stylesheet request or a templating dependency — matching this codebase's
#: existing aversion to new dependencies for one small HTML surface (see
#: merge_confirmation.py's own reasoning). Kept purely presentational: no
#: functional markup lives in here, so a future design pass can replace it
#: without touching any of the render functions below.
_STYLE = """
    :root {
        --aida-accent: #4f46e5;
        --aida-accent-dark: #4338ca;
        --aida-danger: #dc2626;
        --aida-danger-dark: #b91c1c;
        --aida-ink: #1f2430;
        --aida-muted: #6b7280;
        --aida-border: #dfe1e8;
        --aida-bg: #f4f5f8;
        --aida-card: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
        font-family: -apple-system, "Segoe UI", Inter, Roboto, sans-serif;
        background: var(--aida-bg);
        color: var(--aida-ink);
        margin: 0;
        padding: 3rem 1.25rem;
        line-height: 1.55;
    }
    .aida-card {
        max-width: 640px;
        margin: 0 auto;
        background: var(--aida-card);
        border: 1px solid var(--aida-border);
        border-radius: 14px;
        padding: 2rem 2.25rem 2.25rem;
        box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 12px 28px rgba(16, 24, 40, 0.06);
    }
    .aida-kicker {
        display: inline-block;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--aida-accent);
        margin-bottom: 0.6rem;
    }
    h1 {
        font-size: 1.5rem;
        margin: 0 0 0.9rem;
        letter-spacing: -0.01em;
    }
    p { color: var(--aida-ink); }
    p.aida-muted, .aida-muted { color: var(--aida-muted); font-size: 0.92rem; }
    label {
        display: block;
        font-weight: 600;
        font-size: 0.85rem;
        color: var(--aida-ink);
        margin-bottom: 0.35rem;
    }
    input, select, textarea {
        width: 100%;
        font: inherit;
        color: var(--aida-ink);
        background: #fff;
        border: 1px solid var(--aida-border);
        border-radius: 8px;
        padding: 0.55rem 0.7rem;
        transition: border-color 0.15s, box-shadow 0.15s;
    }
    input:focus, select:focus, textarea:focus {
        outline: none;
        border-color: var(--aida-accent);
        box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.15);
    }
    textarea { resize: vertical; }
    .aida-field { margin: 0 0 1.1rem; }
    button {
        font: inherit;
        font-weight: 600;
        border: none;
        border-radius: 8px;
        padding: 0.65rem 1.35rem;
        cursor: pointer;
        color: #fff;
        background: var(--aida-accent);
        transition: background 0.15s;
    }
    button:hover { background: var(--aida-accent-dark); }
    button.aida-danger { background: var(--aida-danger); }
    button.aida-danger:hover { background: var(--aida-danger-dark); }
    a { color: var(--aida-accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code {
        background: #f1f2f6;
        border-radius: 4px;
        padding: 0.1rem 0.35rem;
        font-size: 0.9em;
    }
    .aida-error {
        background: #fef2f2;
        border-left: 3px solid var(--aida-danger);
        color: #991b1b;
        padding: 0.7rem 0.9rem;
        border-radius: 6px;
        font-size: 0.92rem;
        margin: 0 0 1.1rem;
    }
    .aida-summary {
        background: #f7f7fb;
        border: 1px solid var(--aida-border);
        border-radius: 10px;
        padding: 0.9rem 1.1rem;
        margin: 0.9rem 0 1.3rem;
    }
    .aida-summary p { margin: 0.2rem 0; }
    .aida-actions { margin-top: 1.4rem; display: flex; align-items: center; gap: 1.1rem; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    # `no-referrer` matches merge_confirmation.py/comment_deletion.py — this
    # module's own delete-confirmation route also embeds an id in its URL.
    # Security-audit finding: this page was missing it while the other two
    # already had it, an inconsistency in an otherwise-established pattern.
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta name='referrer' content='no-referrer'>"
        f"<title>{html.escape(title)}</title>"
        f"<style>{_STYLE}</style></head>"
        f"<body><div class='aida-card'>"
        f"<span class='aida-kicker'>AIDA-MATE</span>"
        f"{body}</div></body></html>"
    )


def _form_page(
    *,
    error: str | None = None,
    prompt: str = "",
    repository: str = "",
    frequency: str = "daily",
    run_on_date: str = "",
    run_at_time: str = "",
    day_of_week: str = "0",
    day_of_month: str = "1",
    interval_hours: str = "1",
) -> HTMLResponse:
    error_html = f"<div class='aida-error'>{html.escape(error)}</div>" if error else ""
    frequency_options = "".join(
        f"<option value='{value}'{' selected' if frequency == value else ''}>{label}</option>"
        for value, label in [
            ("once", "Once"),
            ("daily", "Daily"),
            ("weekly", "Weekly"),
            ("hourly", "Hourly"),
            ("monthly", "Monthly"),
        ]
    )
    weekday_options = "".join(
        f"<option value='{i}'{' selected' if day_of_week == str(i) else ''}>{label}</option>"
        for i, label in enumerate(_WEEKDAY_LABELS)
    )
    body = f"""
        <h1>New Scheduled Prompt</h1>
        <p class="aida-muted">Runs against a snapshot of the given repository on the schedule below (IST), and
        posts its result as a comment on the <strong>AIDA-MATE Scheduled Prompts</strong>
        dashboard issue in Linear.</p>
        {error_html}
        <form method="post" action="/scheduled-prompts/new">
            <div class="aida-field">
                <label for="prompt">Prompt</label>
                <textarea id="prompt" name="prompt" rows="4" required
                >{html.escape(prompt)}</textarea>
            </div>
            <div class="aida-field">
                <label for="repository">GitHub repository or pull request link</label>
                <input id="repository" name="repository" type="url"
                       value="{html.escape(repository)}"
                       placeholder="https://github.com/owner/repo or .../pull/123" required>
            </div>
            <div class="aida-field">
                <label for="frequency">Frequency</label>
                <select id="frequency" name="frequency" onchange="aidaMateUpdateFrequencyFields()">
                    {frequency_options}
                </select>
            </div>
            <div id="field-run_on_date" class="freq-field aida-field">
                <label for="run_on_date">Date</label>
                <input id="run_on_date" name="run_on_date" type="date"
                       value="{html.escape(run_on_date)}">
            </div>
            <div id="field-interval_hours" class="freq-field aida-field">
                <label for="interval_hours">Every how many hours (1-23)</label>
                <input id="interval_hours" name="interval_hours" type="number" min="1" max="23"
                       value="{html.escape(interval_hours)}">
            </div>
            <div id="field-day_of_week" class="freq-field aida-field">
                <label for="day_of_week">Day of week</label>
                <select id="day_of_week" name="day_of_week">{weekday_options}</select>
            </div>
            <div id="field-day_of_month" class="freq-field aida-field">
                <label for="day_of_month">Day of month (1-31)</label>
                <input id="day_of_month" name="day_of_month" type="number" min="1" max="31"
                       value="{html.escape(day_of_month)}">
            </div>
            <div id="field-run_at_time" class="freq-field aida-field">
                <label for="run_at_time">Time (IST, 24h)</label>
                <input id="run_at_time" name="run_at_time" type="time"
                       value="{html.escape(run_at_time)}">
            </div>
            <div class="aida-actions">
                <button type="submit">Create schedule</button>
            </div>
        </form>
        <script>
            function aidaMateUpdateFrequencyFields() {{
                var freq = document.getElementById('frequency').value;
                var shown = {{
                    once: ['field-run_on_date', 'field-run_at_time'],
                    hourly: ['field-interval_hours'],
                    daily: ['field-run_at_time'],
                    weekly: ['field-day_of_week', 'field-run_at_time'],
                    monthly: ['field-day_of_month', 'field-run_at_time'],
                }}[freq] || [];
                document.querySelectorAll('.freq-field').forEach(function (el) {{
                    el.style.display = shown.indexOf(el.id) !== -1 ? 'block' : 'none';
                }});
            }}
            aidaMateUpdateFrequencyFields();
        </script>
    """
    return _page("AIDA-MATE — New Scheduled Prompt", body)


def _target_description(scheduled) -> str:
    """'PR #123 in `owner/repo`' when targeting a PR, else '`owner/repo`'."""
    repository = f"<code>{html.escape(scheduled.repository)}</code>"
    if scheduled.pr_number is not None:
        return f"PR #{scheduled.pr_number} in {repository}"
    return repository


def _success_page(created) -> HTMLResponse:
    body = f"""
        <h1>Scheduled</h1>
        <div class="aida-summary">
            <p><strong>{html.escape(created.title)}</strong></p>
            <p class="aida-muted">Runs {html.escape(_render_schedule(created))}
            against {_target_description(created)}</p>
        </div>
        <p class="aida-muted">Its result will be posted on the AIDA-MATE Scheduled Prompts dashboard issue in
        Linear.</p>
        <div class="aida-actions"><a href="/scheduled-prompts/new">Create another →</a></div>
    """
    return _page("AIDA-MATE — Scheduled", body)


def _not_found_page() -> HTMLResponse:
    return _page(
        "AIDA-MATE — Not found",
        "<h1>Not found</h1><p class='aida-muted'>No such scheduled prompt — it may already be deleted.</p>",
    )


def _confirm_delete_page(scheduled: ScheduledPrompt) -> HTMLResponse:
    body = f"""
        <h1>Delete this scheduled prompt?</h1>
        <div class="aida-summary">
            <p><strong>{html.escape(scheduled.title)}</strong></p>
            <p class="aida-muted">Repository: {_target_description(scheduled)}</p>
            <p class="aida-muted">Runs {html.escape(_render_schedule(scheduled))}</p>
        </div>
        <p class="aida-muted">It will stop running and be removed from the dashboard.
        This cannot be undone.</p>
        <form method="post" action="/scheduled-prompts/{html.escape(scheduled.id)}/delete">
            <div class="aida-actions">
                <button type="submit" class="aida-danger">Delete</button>
                <a href="/scheduled-prompts/new">Cancel</a>
            </div>
        </form>
    """
    return _page("AIDA-MATE — Delete Scheduled Prompt", body)


def _deleted_page(title: str) -> HTMLResponse:
    body = f"""
        <h1>Deleted</h1>
        <p><strong>{html.escape(title)}</strong> has been removed and will no longer run.</p>
        <div class="aida-actions"><a href="/scheduled-prompts/new">Create a new scheduled prompt →</a></div>
    """
    return _page("AIDA-MATE — Deleted", body)


def _derive_title(prompt: str) -> str:
    """A schedule needs a title for the dashboard table; this form only asks
    for the prompt itself, so one is derived from it rather than adding a
    field the user didn't ask for. The full prompt (whitespace collapsed) is
    used verbatim, untruncated, so the dashboard always shows the complete
    title rather than a cut-off fragment."""
    return " ".join(prompt.split())


@router.get("/new", response_class=HTMLResponse)
async def new_scheduled_prompt_form() -> HTMLResponse:
    """Show the creation form."""
    return _form_page()


@router.post("/new", response_class=HTMLResponse)
async def submit_scheduled_prompt_form(
    request: Request,
    prompt: str = Form(...),
    repository: str = Form(...),
    frequency: str = Form("daily"),
    run_on_date: str = Form(""),
    run_at_time: str = Form(""),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    interval_hours: str = Form(""),
) -> HTMLResponse:
    """Handle the form submission: resolve the org, ensure a dashboard issue
    exists, then create the schedule through the same path the JSON API uses.
    """

    def _redisplay(error: str) -> HTMLResponse:
        return _form_page(
            error=error,
            prompt=prompt,
            repository=repository,
            frequency=frequency,
            run_on_date=run_on_date,
            run_at_time=run_at_time,
            day_of_week=day_of_week or "0",
            day_of_month=day_of_month or "1",
            interval_hours=interval_hours or "1",
        )

    organization_id = await _resolve_organization_id(request, None)
    if organization_id is None:
        return _redisplay("Could not resolve a single Linear workspace — connect one via Linear OAuth first.")

    dashboard_service = getattr(request.app.state, "scheduled_prompt_dashboard_service", None)
    if dashboard_service is None:
        return _redisplay("The scheduled-prompts dashboard isn't configured — set LINEAR_SYNC_TEAM_KEY.")

    dashboard = await dashboard_service.ensure(organization_id)
    if dashboard is None:
        return _redisplay(
            "Could not create the dashboard issue — check that LINEAR_SYNC_TEAM_KEY resolves to a real team."
        )

    parsed_repository = _parse_github_repository(repository)
    if parsed_repository is None:
        return _redisplay("Repository must be a GitHub URL, like https://github.com/owner/repo.")

    try:
        parsed_day_of_week = _to_int(day_of_week)
        parsed_day_of_month = _to_int(day_of_month)
        parsed_interval_hours = _to_int(interval_hours)
    except ValueError:
        return _redisplay("Day of week/month and hour interval must be whole numbers.")

    try:
        body = ScheduledPromptCreate(
            title=_derive_title(prompt),
            prompt=prompt,
            repository=parsed_repository,
            pr_number=_parse_pr_number(repository),
            frequency=frequency,
            run_on_date=run_on_date or None,
            interval_hours=parsed_interval_hours,
            day_of_week=parsed_day_of_week,
            day_of_month=parsed_day_of_month,
            run_at_time=run_at_time or None,
            timezone=_IST,
            linear_issue_id=dashboard.linear_issue_id,
            organization_id=organization_id,
        )
    except ValidationError as exc:
        return _redisplay(exc.errors()[0]["msg"])

    try:
        created = await create_scheduled_prompt(request, body)
    except HTTPException as exc:
        return _redisplay(str(exc.detail))

    return _success_page(created)


@router.get("/{scheduled_id}/delete", response_class=HTMLResponse)
async def confirm_delete_scheduled_prompt(request: Request, scheduled_id: str) -> HTMLResponse:
    """Show a confirmation page for deleting `scheduled_id`.

    A GET here only reads and renders — never deletes. That's what makes it
    safe for the dashboard's per-row "Delete" link to point directly here:
    the same GET-shows/POST-acts split `merge_confirmation.py` uses, so a
    link preview or crawler fetching this URL can't trigger a real deletion.
    """
    scheduled = await request.app.state.scheduled_prompt_repository.get(scheduled_id)
    if scheduled is None:
        return _not_found_page()
    return _confirm_delete_page(scheduled)


@router.post("/{scheduled_id}/delete", response_class=HTMLResponse)
async def submit_delete_scheduled_prompt(request: Request, scheduled_id: str) -> HTMLResponse:
    """Handle the confirmation form's submit: delete via the same path the
    JSON API's `DELETE /scheduled-prompts/{id}` uses, which already re-syncs
    the dashboard afterward — no separate sync call needed here."""
    scheduled = await request.app.state.scheduled_prompt_repository.get(scheduled_id)
    if scheduled is None:
        return _not_found_page()
    await delete_scheduled_prompt(request, scheduled_id)
    return _deleted_page(scheduled.title)
