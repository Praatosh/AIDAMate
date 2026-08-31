"""The delete-link confirmation page for AIDA-MATE-posted Linear comments.

Every comment `LinearService.add_comment` posts carries a link here (see
CLAUDE.md's comment-deletion section). Same shape as
`app/api/merge_confirmation.py`: GET only reads and renders, never deletes —
what makes it safe for the link embedded directly in a Linear comment (a
link preview/crawler fetching it can't trigger a real deletion); POST (a
human's "Delete" button click) does the actual work.

No auth layer: the only "secret" in the URL is the token — a `uuid4`
(~122 bits), the same "unguessable token, not a real auth check" pattern as
`review_id` in `merge_confirmation.py` and `scheduled_id` in
`scheduled_prompt_form.py`'s delete flow.

This module owns its own tiny `_page()` render helper rather than importing
`merge_confirmation.py`'s — matching the existing precedent that each HTML
page in this app has its own (see `scheduled_prompt_form.py`'s equivalent).
Styling here is a copy of `scheduled_prompt_form.py`'s `_STYLE`, not a shared
import, for the same reason: this app deliberately keeps its handful of HTML
pages independent rather than introducing a shared-template dependency for
them — each one's CSS can drift or be tuned on its own without touching the
others.
"""

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.errors import LinearError
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/comments", tags=["comment-deletion"])

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
        max-width: 520px;
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
        font-size: 1.4rem;
        margin: 0 0 0.7rem;
        letter-spacing: -0.01em;
    }
    p { color: var(--aida-ink); margin: 0 0 1.2rem; }
    p.aida-muted { color: var(--aida-muted); font-size: 0.92rem; }
    button {
        font: inherit;
        font-weight: 600;
        border: none;
        border-radius: 8px;
        padding: 0.65rem 1.35rem;
        cursor: pointer;
        color: #fff;
        background: var(--aida-danger);
        transition: background 0.15s;
    }
    button:hover { background: var(--aida-danger-dark); }
    .aida-error-box {
        background: #fef2f2;
        border-left: 3px solid var(--aida-danger);
        color: #991b1b;
        padding: 0.7rem 0.9rem;
        border-radius: 6px;
        font-size: 0.92rem;
        margin: 0 0 1.2rem;
        word-break: break-word;
    }
"""


def _page(title: str, body: str) -> HTMLResponse:
    # This page's own URL embeds an unguessable token, same reasoning as
    # merge_confirmation.py's _page — no referrer leak on any outbound link.
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


def _not_found_page() -> HTMLResponse:
    return _page(
        "AIDA-MATE — nothing here",
        "<h1>Nothing here</h1>"
        "<p class='aida-muted'>This link is invalid, or the comment was already deleted.</p>",
    )


def _confirmation_page(token: str) -> HTMLResponse:
    body = f"""
        <h1>Delete this comment?</h1>
        <p class="aida-muted">This deletes the AIDA-MATE comment this link was posted with in Linear.
        This cannot be undone.</p>
        <form method="post" action="/comments/{html.escape(token)}/delete">
            <button type="submit">Delete comment</button>
        </form>
    """
    return _page("AIDA-MATE — delete comment", body)


def _deleted_page() -> HTMLResponse:
    body = """
        <h1>Deleted</h1>
        <p class="aida-muted">The comment has been removed from Linear.</p>
    """
    return _page("AIDA-MATE — deleted", body)


def _error_page(message: str) -> HTMLResponse:
    body = f"""
        <h1>Could not delete</h1>
        <div class="aida-error-box">{html.escape(message)}</div>
        <p class="aida-muted">The comment is still there — this link is still valid, so you can try again.</p>
    """
    return _page("AIDA-MATE — could not delete", body)


@router.get("/{token}/delete", response_class=HTMLResponse)
async def get_comment_deletion(request: Request, token: str) -> HTMLResponse:
    """Show the confirmation dialog for deleting a posted comment."""
    repository = request.app.state.posted_comment_repository
    record = await repository.get(token) if repository is not None else None
    if record is None:
        return _not_found_page()
    return _confirmation_page(token)


@router.post("/{token}/delete", response_class=HTMLResponse)
async def post_comment_deletion(request: Request, token: str) -> HTMLResponse:
    """Delete the comment via the confirmation dialog's "Delete" button."""
    repository = request.app.state.posted_comment_repository
    linear_service = request.app.state.linear_service
    record = await repository.get(token) if repository is not None else None
    if record is None:
        return _not_found_page()

    try:
        await linear_service.delete_comment(record.linear_comment_id, organization_id=record.organization_id)
    except LinearError as exc:
        # The record is deliberately left in place on failure — the link
        # still works, so a retry (or a human trying again later) can pick
        # up where this attempt left off.
        logger.warning(
            "Could not delete Linear comment",
            extra={"linear_comment_id": record.linear_comment_id, "error": str(exc)},
        )
        return _error_page(str(exc))

    await repository.delete(token)
    return _deleted_page()
