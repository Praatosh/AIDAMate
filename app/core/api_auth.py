"""Shared-secret authentication for AIDA-MATE's own JSON management API.

Protects `/reviews*` (`app/api/reviews.py`), `/scheduled-prompts*`
(`app/api/scheduled_prompts.py`), and `GET /auth/linear/status`
(`app/api/linear_auth.py`, applied per-route rather than at the router
level). Every other HTTP entry point already authenticates a different way
and is deliberately left alone here:

* The two webhooks (`app/api/linear_webhook.py`, `app/api/github_webhook.py`)
  verify an HMAC signature over the raw request body.
* The three human-facing HTML pages (`merge_confirmation.py`,
  `comment_deletion.py`, `scheduled_prompt_form.py`'s delete-confirmation
  route) gate on an unguessable bearer token embedded in their own URL —
  requiring an API key there too would break the "click the link Linear
  posted" flow they exist for.
* `/auth/linear/install` and `/auth/linear/callback` must stay
  browser-accessible for the OAuth redirect flow itself — a human's browser
  hitting these has no way to attach an `X-Api-Key` header.

`/scheduled-prompts/new` (the create form) and its POST handler are also
unaffected: `scheduled_prompt_form.py` calls `create_scheduled_prompt`/
`delete_scheduled_prompt` as plain Python functions, not by re-entering
`scheduled_prompts.router`'s own routing — a router-level dependency only
ever runs for requests that actually go through that router.

This was a real gap, not a hardening exercise: `/scheduled-prompts` in
particular is a full read/write/delete surface with no restriction on
`linear_issue_id` or `prompt` content, reachable by anyone who could reach
the host. See CLAUDE.md's security-audit note for the finding this fixes.
"""

import hmac

from fastapi import Header, HTTPException, Request, status

#: Header name clients must send the configured key as.
API_KEY_HEADER = "X-Api-Key"


async def require_management_api_key(
    request: Request, x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)
) -> None:
    """FastAPI dependency: reject unless `x_api_key` matches `MANAGEMENT_API_KEY`.

    Fail-closed by default — matching this app's own established pattern for
    `GITHUB_WEBHOOK_SECRET` (unset means every webhook delivery is rejected as
    unsigned) and `GITHUB_REPO_ALLOWLIST` (empty means no repository is
    allowed). An unconfigured `MANAGEMENT_API_KEY` must not silently leave
    these endpoints open — that would just be the same vulnerability with
    extra steps.

    Compared with `hmac.compare_digest` for the same timing-safety reason the
    webhook signature checks use it (see `linear_webhook.py`/
    `github_webhook.py`): this key is a bearer credential too, so the same
    class of attack — an adaptive timing measurement across repeated guesses
    — applies equally to a naive `==`.
    """
    settings = request.app.state.settings
    configured = settings.management_api_key
    if not configured or not x_api_key or not hmac.compare_digest(x_api_key, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Api-Key.",
        )
