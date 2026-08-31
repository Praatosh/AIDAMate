"""The gated auto-merge confirmation page. See CLAUDE.md §1a.

The only HTML surface in AIDA-MATE — everywhere else in this app is JSON.
Exists because a MEDIUM/HIGH-risk merge must never happen without an explicit
human "Yes, merge" click; the link a human follows to give that click is
posted as a Linear comment by `app/services/auto_merge_service.py`.

No auth layer: the only "secret" in the URL is the path segment (named
`token` below), a `uuid4` (~122 bits) — the same "unguessable token, not a
real auth check" pattern already used for the OAuth `state` parameter
(`app/models/linear.py`). Security-audit finding, fixed: this used to be
`job.id`, but `id` is also returned by the unauthenticated `GET /reviews`
listing, so it wasn't actually secret — a MEDIUM/HIGH-risk merge could be
triggered by anyone who could read that listing, with no confirmation from a
human at all. It's now `ReviewJob.merge_confirmation_token`, a separate value
minted only when a job enters `PENDING_CONFIRMATION`
(`ReviewJob.mark_merge_pending`) and never returned by any listing endpoint —
the only place it's ever handed out is the Linear comment the confirmation
link itself is posted in. No new dependency (Jinja2/templates) for one page —
small f-string render functions, matching this codebase's stated aversion to
new dependencies for narrow needs.

Every value interpolated into these pages is `html.escape()`-d. A `Finding`'s
`description`/`reason` can originate from LLM output, so this is the one
place in the app that renders that text as HTML instead of returning it as
JSON — the one place cross-site scripting is a real, not theoretical, risk.
"""

import html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.core.errors import AidaMateError
from app.core.logging import get_logger
from app.models.common import MergeStatus
from app.models.review import ReviewJob

logger = get_logger(__name__)

router = APIRouter(prefix="/reviews", tags=["merge-confirmation"])


def _page(title: str, body: str) -> HTMLResponse:
    # `review_id` (an unguessable bearer token, per this module's own
    # docstring) is embedded in this page's own URL. Without a referrer
    # policy, following the outbound link to GitHub could leak that URL to
    # GitHub via the Referer header — modern browsers' default
    # (strict-origin-when-cross-origin) already truncates it to just the
    # origin on a cross-origin navigation, but this makes it explicit rather
    # than relying on a default that could change. Security-audit finding.
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='referrer' content='no-referrer'>"
        f"<title>{html.escape(title)}</title></head>"
        f"<body style='font-family: sans-serif; max-width: 640px; margin: 2rem auto; line-height: 1.5;'>"
        f"{body}</body></html>"
    )


def _not_pending_page() -> HTMLResponse:
    return _page(
        "AIDA-MATE — nothing pending",
        "<h1>Nothing pending here</h1>"
        "<p>This review either doesn't exist, or its merge confirmation was already decided.</p>",
    )


def _outcome_page(job: ReviewJob) -> HTMLResponse:
    if job.merge_status is MergeStatus.MERGED:
        return _page("AIDA-MATE — merged", "<h1>Merged</h1><p>The pull request has been merged.</p>")
    if job.merge_status is MergeStatus.DECLINED:
        return _page(
            "AIDA-MATE — declined",
            "<h1>Left open</h1><p>No action was taken. The pull request remains open.</p>",
        )
    if job.merge_status is MergeStatus.FAILED:
        return _page(
            "AIDA-MATE — merge failed",
            f"<h1>Could not merge</h1><p>{html.escape(job.merge_error or 'Unknown error.')}</p>",
        )
    return _not_pending_page()


def _confirmation_page(job: ReviewJob, token: str) -> HTMLResponse:
    result = job.result
    assert result is not None  # guaranteed by the PENDING_CONFIRMATION invariant

    areas = ", ".join(sorted(area.value for area in result.areas)) or "none detected"
    findings_html = "".join(
        f"<li>[{html.escape(finding.category.value)}/{html.escape(finding.severity.value)}] "
        f"{html.escape(finding.description)}</li>"
        for finding in result.findings
    ) or "<li>No specific findings recorded.</li>"

    pr = job.pull_request
    pr_html = (
        f"<a href='{html.escape(pr.url)}' rel='noreferrer'>{html.escape(pr.slug)}</a>" if pr else "unknown"
    )

    body = f"""
        <h1>Merge confirmation needed — risk {html.escape(result.risk.value)}</h1>
        <p>Pull request: {pr_html}</p>
        <p>Affected areas: {html.escape(areas)}</p>
        <p>Findings:</p>
        <ul>{findings_html}</ul>
        <p><strong>Have you re-checked the code related to this? If not, go check once more
        before merging.</strong></p>
        <form method="post" action="/reviews/{html.escape(token)}/merge-confirm">
            <button type="submit" name="decision" value="yes">Yes, merge</button>
            <button type="submit" name="decision" value="no">No</button>
        </form>
    """
    return _page("AIDA-MATE — merge confirmation", body)


@router.get("/{token}/merge-confirm", response_class=HTMLResponse)
async def get_merge_confirmation(request: Request, token: str) -> HTMLResponse:
    """Show the confirmation dialog for a pending MEDIUM/HIGH-risk merge.

    `token` is `ReviewJob.merge_confirmation_token`, not `job.id` — see this
    module's docstring for why.
    """
    job = await request.app.state.job_repository.find_by_merge_confirmation_token(token)
    if job is None or job.merge_status is not MergeStatus.PENDING_CONFIRMATION:
        return _not_pending_page()
    return _confirmation_page(job, token)


@router.post("/{token}/merge-confirm", response_class=HTMLResponse)
async def post_merge_confirmation(request: Request, token: str, decision: str = Form(...)) -> HTMLResponse:
    """Resolve a pending merge confirmation from the dialog's Yes/No buttons."""
    service = request.app.state.auto_merge_service
    if service is None:
        return _not_pending_page()
    try:
        job = await service.confirm(token, approved=decision == "yes")
    except AidaMateError:
        return _not_pending_page()
    return _outcome_page(job)
