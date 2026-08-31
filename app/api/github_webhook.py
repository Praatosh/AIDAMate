"""GitHub webhook intake. See CLAUDE.md §1b and §1c.

AIDA-MATE's first *inbound* GitHub webhook receiver — `app/services/github_service.py`
is a pure outbound REST client; nothing before this ever accepted a delivery
FROM GitHub. Handler sequence mirrors `app/api/linear_webhook.py`'s own
discipline (verify -> parse -> filter -> dispatch -> return 2xx fast), adapted
for GitHub's conventions:

* GitHub signs with `X-Hub-Signature-256: sha256=<hex>` — a prefixed digest,
  unlike Linear's bare hex `Linear-Signature`.
* No per-event timestamp in the body, so there is no freshness/replay check
  here the way `is_fresh()` exists for Linear. Not adding delivery-id dedup
  storage either: both `AutoMergeService`/`GitHubMergeSyncService`/
  `GitHubIssueSyncService` are idempotent by construction (§1a/§1b/§1c), so a
  redelivered event just repeats a harmless no-op rather than needing to be
  detected and dropped up front.

GitHub delivers every event type a webhook is subscribed to at the SAME
configured URL — there is exactly one inbound endpoint here, distinguished by
the `X-GitHub-Event` header, not one endpoint per feature. Two independently
gated categories are handled, each with its own flag checked before any
parsing happens (so a disabled feature costs nothing, same discipline as the
Linear side's `settings.auto_merge_on_done_enabled` check):

* `pull_request` (§1b, `GITHUB_MERGE_SYNC_ENABLED`) — only a merge
  (`action: closed` AND `pull_request.merged: true`) into the configured base
  branch, for an allowlisted repository.
* `issues` / `code_scanning_alert` / `dependabot_alert` / `secret_scanning_alert`
  (§1c, `GITHUB_ISSUE_SYNC_ENABLED`) — every delivery for an allowlisted
  repository; no action-based filtering, since create-or-update is idempotent
  either way and every delivery is a genuine signal, not noise to filter.

Everything else (PRs by themselves outside a merge, CI/Actions runs, checks,
any other event type) gets a 2xx "ignored" ack, same philosophy as Linear's
unrelated-event handling — never synced, regardless of any flag.
"""

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.github import GitHubIssueEvent, PullRequestRef, RepositoryRef, SecurityAlertEvent

logger = get_logger(__name__)

router = APIRouter(tags=["webhooks"])

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"

_PULL_REQUEST_EVENT = "pull_request"
_CLOSED_ACTION = "closed"

_ISSUE_EVENT = "issues"
_CODE_SCANNING_EVENT = "code_scanning_alert"
_DEPENDABOT_EVENT = "dependabot_alert"
_SECRET_SCANNING_EVENT = "secret_scanning_alert"
_ISSUE_SYNC_EVENTS = frozenset(
    {_ISSUE_EVENT, _CODE_SCANNING_EVENT, _DEPENDABOT_EVENT, _SECRET_SCANNING_EVENT}
)

#: event type -> the SecurityAlertEvent.source_type it maps onto.
_SECURITY_SOURCE_TYPES = {
    _CODE_SCANNING_EVENT: "code_scan",
    _DEPENDABOT_EVENT: "dependabot",
    _SECRET_SCANNING_EVENT: "secret_scan",
}


class WebhookAck(BaseModel):
    """Response body for a webhook delivery."""

    accepted: bool
    reason: str | None = None


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub's `sha256=<hex>` HMAC-SHA256 signature over the raw request body.

    Compared with `hmac.compare_digest` for the same timing-safety reason as
    Linear's `verify_signature` in `app/api/linear_webhook.py`.
    """
    if not signature_header or not secret:
        return False

    algo, _, digest = signature_header.strip().partition("=")
    if algo != "sha256" or not digest:
        return False

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a payload fragment to a dict, tolerating nulls and wrong types."""
    return value if isinstance(value, dict) else {}


def _repository_ref(payload: dict[str, Any]) -> RepositoryRef | None:
    """Build a `RepositoryRef` from any of these webhooks' shared `repository` field."""
    repository = _as_dict(payload.get("repository"))
    owner = _as_dict(repository.get("owner")).get("login")
    name = repository.get("name")
    if not owner or not name:
        return None
    return RepositoryRef(owner=owner, name=name)


def _pull_request_ref(payload: dict[str, Any]) -> PullRequestRef | None:
    """Build a `PullRequestRef` directly from a `pull_request` webhook payload.

    Returns None on a malformed payload rather than raising — a hostile or
    truncated body should be ignored, not crash the handler.
    """
    repo = _repository_ref(payload)
    pull_request = _as_dict(payload.get("pull_request"))
    number = pull_request.get("number")
    url = pull_request.get("html_url")

    if repo is None or not number or not url:
        return None

    return PullRequestRef(repository=repo, number=number, url=url)


def _github_issue_event(payload: dict[str, Any]) -> GitHubIssueEvent | None:
    """Build a `GitHubIssueEvent` from an `issues` webhook payload.

    `issues` events only ever carry a genuine Issue — unlike the REST
    `/issues` list endpoint, which mixes PRs in, GitHub's webhook payload for
    this event type has no PR-disguise case to filter out.
    """
    repo = _repository_ref(payload)
    issue = _as_dict(payload.get("issue"))
    number = issue.get("number")
    title = issue.get("title")
    url = issue.get("html_url")
    if repo is None or not number or not title or not url:
        return None

    labels = [
        name for label in (issue.get("labels") or []) if (name := _as_dict(label).get("name"))
    ]

    return GitHubIssueEvent(
        number=number,
        title=title,
        body=issue.get("body"),
        state=issue.get("state") or "open",
        labels=labels,
        author_login=_as_dict(issue.get("user")).get("login"),
        repository=repo,
        url=url,
    )


def _security_alert_event(payload: dict[str, Any], event_type: str) -> SecurityAlertEvent | None:
    """Build a `SecurityAlertEvent` from a code_scanning_alert / dependabot_alert /
    secret_scanning_alert webhook payload.

    Field extraction here is built from GitHub's documented webhook schemas,
    not verified against live deliveries — this repo has none of the three
    source features enabled (confirmed while planning this). Treat the exact
    `details` keys as the first thing to check against a real payload if
    synced issue content ever looks wrong.
    """
    repo = _repository_ref(payload)
    alert = _as_dict(payload.get("alert"))
    number = alert.get("number")
    url = alert.get("html_url")
    if repo is None or not number or not url:
        return None

    source_type = _SECURITY_SOURCE_TYPES[event_type]
    state = alert.get("state") or "open"
    commit_sha: str | None = None
    ref: str | None = None
    details: dict[str, Any] = {}

    if source_type == "code_scan":
        instance = _as_dict(alert.get("most_recent_instance"))
        rule = _as_dict(alert.get("rule"))
        location = _as_dict(instance.get("location"))
        commit_sha = instance.get("commit_sha")
        ref = instance.get("ref")
        details = {
            "rule_description": rule.get("description") or rule.get("id"),
            "severity": rule.get("security_severity_level") or rule.get("severity"),
            "description": _as_dict(instance.get("message")).get("text"),
            "file": location.get("path"),
            "line": location.get("start_line"),
        }
    elif source_type == "dependabot":
        # No commit SHA: a Dependabot alert is about the dependency manifest
        # on the default branch, not a specific commit — no PR relationship
        # is attempted for this source unless GitHub adds one to the payload.
        package = _as_dict(_as_dict(alert.get("dependency")).get("package"))
        advisory = _as_dict(alert.get("security_advisory"))
        vulnerability = _as_dict(alert.get("security_vulnerability"))
        first_patched = _as_dict(vulnerability.get("first_patched_version"))
        details = {
            "package": package.get("name"),
            "ecosystem": package.get("ecosystem"),
            "severity": advisory.get("severity"),
            "vulnerability": advisory.get("summary"),
            "vulnerable_range": vulnerability.get("vulnerable_version_range"),
            "fixed_version": first_patched.get("identifier"),
        }
    elif source_type == "secret_scan":
        details = {"secret_type": alert.get("secret_type_display_name") or alert.get("secret_type")}

    return SecurityAlertEvent(
        source_type=source_type,
        alert_number=number,
        state=state,
        repository=repo,
        url=url,
        commit_sha=commit_sha,
        ref=ref,
        details={key: value for key, value in details.items() if value is not None},
    )


@router.post("/webhooks/github", response_model=WebhookAck)
async def handle_github_webhook(request: Request, response: Response) -> WebhookAck:
    """Receive a GitHub webhook.

    Returns 2xx quickly in every non-auth case — a 4xx would make GitHub
    retry traffic AIDA-MATE simply does not want.
    """
    settings: Settings = request.app.state.settings

    raw_body = await request.body()

    secret = settings.github_webhook_secret or ""
    if not verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER), secret):
        logger.warning("Rejected GitHub webhook with invalid signature")
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return WebhookAck(accepted=False, reason="invalid_signature")

    event_type = request.headers.get(EVENT_HEADER)

    # Each category's flag is checked before any parsing, so a disabled
    # feature costs nothing — no JSON parse, no lookups, no Linear calls.
    if event_type == _PULL_REQUEST_EVENT:
        if not settings.github_merge_sync_enabled:
            response.status_code = status.HTTP_202_ACCEPTED
            return WebhookAck(accepted=True, reason="sync_disabled")
    elif event_type in _ISSUE_SYNC_EVENTS:
        if not settings.github_issue_sync_enabled:
            response.status_code = status.HTTP_202_ACCEPTED
            return WebhookAck(accepted=True, reason="sync_disabled")
    else:
        response.status_code = status.HTTP_202_ACCEPTED
        return WebhookAck(accepted=True, reason="event_ignored")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        logger.warning("Rejected GitHub webhook with unparseable payload")
        response.status_code = status.HTTP_400_BAD_REQUEST
        return WebhookAck(accepted=False, reason="invalid_payload")

    if not isinstance(payload, dict):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return WebhookAck(accepted=False, reason="invalid_payload")

    if event_type == _PULL_REQUEST_EVENT:
        await _dispatch_pull_request(request, settings, payload)
    else:
        await _dispatch_issue_sync(request, settings, event_type, payload)

    response.status_code = status.HTTP_202_ACCEPTED
    return WebhookAck(accepted=True)


async def _dispatch_pull_request(request: Request, settings: Settings, payload: dict[str, Any]) -> None:
    """§1b: sync a merge into the configured base branch to Linear Done."""
    pull_request = _as_dict(payload.get("pull_request"))
    merged_into_base = (
        payload.get("action") == _CLOSED_ACTION
        and pull_request.get("merged") is True
        and _as_dict(pull_request.get("base")).get("ref") == settings.github_merge_sync_branch
    )
    if not merged_into_base:
        return

    ref = _pull_request_ref(payload)
    if ref is None or ref.repository.full_name.lower() not in settings.github_repos:
        return

    logger.info(
        "GitHub merge trigger received",
        extra={"github_pr": ref.slug, "delivery_id": request.headers.get(DELIVERY_HEADER)},
    )

    # Awaited inline, not queued: at most one Linear lookup plus one or two
    # GraphQL calls, well within any reasonable ack budget — this is not a
    # sandboxed review, it doesn't need the review queue's worker pool.
    await request.app.state.github_merge_sync_service.handle_pull_request_merged(ref)


async def _dispatch_issue_sync(
    request: Request, settings: Settings, event_type: str, payload: dict[str, Any]
) -> None:
    """§1c: sync a GitHub Issue or security alert into a Linear issue."""
    service = request.app.state.github_issue_sync_service
    if service is None:
        return

    if event_type == _ISSUE_EVENT:
        issue_event = _github_issue_event(payload)
        if issue_event is None or issue_event.repository.full_name.lower() not in settings.github_repos:
            return
        logger.info(
            "GitHub Issue sync trigger received",
            extra={"github_issue": issue_event.number, "delivery_id": request.headers.get(DELIVERY_HEADER)},
        )
        await service.handle_issue_event(issue_event)
        return

    alert_event = _security_alert_event(payload, event_type)
    if alert_event is None or alert_event.repository.full_name.lower() not in settings.github_repos:
        return
    logger.info(
        "GitHub security alert sync trigger received",
        extra={
            "source_type": alert_event.source_type,
            "alert_number": alert_event.alert_number,
            "delivery_id": request.headers.get(DELIVERY_HEADER),
        },
    )
    await service.handle_security_alert(alert_event)
