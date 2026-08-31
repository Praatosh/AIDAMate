"""GitHub Issues / Security vulnerabilities -> Linear. See CLAUDE.md §1c.

The other half of §1b's family (`app/services/github_merge_sync_service.py`):
that one updates an EXISTING Linear issue's state when a PR merges; this one
CREATES/updates Linear issues that don't otherwise exist, keyed by a
fingerprint rather than by any prior AIDA-MATE review.

    GitHub Issue / Code Scanning / Dependabot / Secret Scanning alert
      -> best-effort relationship lookup (commit SHA -> PR, or a PR
         mentioning "#<issue>") -> fingerprint -> existing mapping?
        -> yes -> update the Linear issue's content
        -> no  -> resolve the target Linear team -> create a Linear issue
                  -> store the mapping

A GitHub Issue closing is additionally synced to Linear immediately (same
webhook delivery, no delay): the linked Linear issue is moved to the team's
completed-type state, reusing the exact `find_done_state_id`/
`update_issue_state` pair §1b already built for PR-merge-closes-Linear-issue.
Scoped to plain Issues only, not security alerts — GitHub's alert states
("fixed"/"dismissed"/"open") don't map onto "closed" as cleanly, and nothing
asked for that yet.

The reverse direction also holds: a synced Linear issue landing on a
completed-type state (`handle_linear_issue_done`, called from the Linear
webhook the same way `AutoMergeService.handle_issue_done` is, but keyed off
`SyncMapping.find_by_linear_issue_id` instead of a `ReviewJob`) closes the
linked GitHub Issue back. The two directions cannot loop: GitHub's own
`issues` webhook only fires on an actual state transition, so closing an
already-closed issue (or moving an already-Done Linear issue to Done again)
is a silent no-op on both sides, not a re-triggering event.

Deliberately excludes everything the user's spec ruled out: pull requests by
themselves, CI/Actions runs, checks, and any GitHub event that isn't one of
these four sources — enforced upstream in `app/api/github_webhook.py`'s
dispatch, not here.
"""

from app.core.errors import GitHubError, LinearError
from app.core.interfaces import ISyncMappingRepository
from app.core.logging import get_logger
from app.models.github import GitHubIssueEvent, RepositoryRef, SecurityAlertEvent
from app.models.linear import ReviewTrigger
from app.models.sync_mapping import SyncMapping
from app.services.default_schedule_service import DefaultRepoScheduleService
from app.services.github_service import GitHubService
from app.services.linear_service import LinearService

logger = get_logger(__name__)

_SOURCE_LABELS = {
    "code_scan": "Code Scanning",
    "dependabot": "Dependabot",
    "secret_scan": "Secret Scanning",
}

#: Linear label names attached at creation time so synced issues are visibly
#: taggable in Linear's UI, not just distinguishable by a title prefix.
#: GitHub Issues get their own label; all three vulnerability sources share
#: one "Security" label — a deliberate choice, not an oversight (confirmed
#: with the user rather than defaulting to per-source labels).
_ISSUE_LABEL_NAME = "GitHub Issue"
_SECURITY_LABEL_NAME = "Security"


def _fingerprint(repository: str, source_type: str, source_id: str) -> str:
    """`github:{owner/repo}:{source_type}:{source_id}` — the dedup key."""
    return f"github:{repository}:{source_type}:{source_id}"


def _render_issue_content(event: GitHubIssueEvent, *, pr_number: int | None) -> tuple[str, str]:
    """Build (title, description) for a synced GitHub Issue."""
    lines = [
        "Source: GitHub",
        f"Repository: {event.repository.full_name}",
        "",
        f"GitHub Issue: #{event.number}",
        f"State: {event.state}",
        f"Labels: {', '.join(event.labels) or 'none'}",
        f"Author: {event.author_login or 'unknown'}",
    ]
    if pr_number is not None:
        lines.append(f"Related PR: #{pr_number}")
    if event.body:
        lines += ["", event.body]
    lines += ["", f"GitHub: {event.url}"]

    return f"[GitHub Issue] {event.title}", "\n".join(lines)


def _render_security_alert_content(event: SecurityAlertEvent, *, pr_number: int | None) -> tuple[str, str]:
    """Build (title, description) for a synced security alert.

    `event.details` carries the source-specific fields extracted by the
    webhook handler (rule/severity/file/line for code scanning; package/
    ecosystem/versions for Dependabot; secret type/location for secret
    scanning) — rendered generically here rather than via three separate
    functions, since the shape (a flat list of "Label: value" lines) is
    identical across all three.
    """
    label = _SOURCE_LABELS[event.source_type]
    severity = event.details.get("severity")
    # `severity` is Any (from GitHub's own JSON payload, whose exact field
    # types aren't verified against live traffic — see the module docstring)
    # — coerced to str before .title() so a malformed/unexpected non-string
    # value (e.g. a numeric severity score) can't turn a webhook delivery
    # into an unhandled 500 instead of a synced issue. `severity` absent
    # (None) still omits the prefix entirely, unchanged.
    severity_prefix = f"{str(severity).title()} severity " if severity else ""
    title = f"[Security] {severity_prefix}{label} alert"

    lines = ["Source: GitHub", f"Repository: {event.repository.full_name}", ""]
    for field_label, key in (
        ("Alert", "rule_description"),
        ("Package", "package"),
        ("Ecosystem", "ecosystem"),
        ("Secret type", "secret_type"),
        ("Severity", "severity"),
        ("Vulnerability", "vulnerability"),
        ("Affected version", "vulnerable_range"),
        ("Fixed version", "fixed_version"),
        ("Description", "description"),
        ("Location", "location"),
        ("File", "file"),
        ("Line", "line"),
    ):
        value = event.details.get(key)
        if value:
            lines.append(f"{field_label}: {value}")

    if event.commit_sha:
        lines.append(f"Commit: {event.commit_sha}")
    if event.ref:
        lines.append(f"Branch: {event.ref}")
    lines.append(f"State: {event.state}")
    if pr_number is not None:
        lines.append(f"Related PR: #{pr_number}")

    lines += ["", f"GitHub: {event.url}"]
    return title, "\n".join(lines)


class GitHubIssueSyncService:
    """Creates/updates Linear issues for GitHub Issues and security alerts.

    `default_schedule_service`, when configured, is asked to ensure a
    default scheduled prompt exists for a repo the moment that repo's
    *first* `SyncMapping` is created — never on an update to an existing
    one. See `app/services/default_schedule_service.py`.
    """

    def __init__(
        self,
        mappings: ISyncMappingRepository,
        github: GitHubService,
        linear: LinearService,
        *,
        team_key: str,
        default_schedule_service: DefaultRepoScheduleService | None = None,
    ) -> None:
        self._mappings = mappings
        self._github = github
        self._linear = linear
        self._team_key = team_key
        self._default_schedule_service = default_schedule_service

    async def handle_issue_event(self, event: GitHubIssueEvent) -> None:
        """React to a GitHub Issue being created, updated, or closed."""
        pr_number = await self._find_pr_referencing_issue(event)
        title, description = _render_issue_content(event, pr_number=pr_number)
        await self._upsert(
            fingerprint=_fingerprint(event.repository.full_name, "issue", str(event.number)),
            source_type="issue",
            source_id=str(event.number),
            repository=event.repository.full_name,
            title=title,
            description=description,
            github_url=event.url,
            state=event.state,
            pr_number=pr_number,
            label_name=_ISSUE_LABEL_NAME,
            close_when_closed=True,
        )

    async def handle_security_alert(self, event: SecurityAlertEvent) -> None:
        """React to a Code Scanning / Dependabot / Secret Scanning alert being created or updated."""
        pr_number = await self._find_pr_for_commit(event) if event.commit_sha else None
        title, description = _render_security_alert_content(event, pr_number=pr_number)
        await self._upsert(
            fingerprint=_fingerprint(event.repository.full_name, event.source_type, str(event.alert_number)),
            source_type=event.source_type,
            source_id=str(event.alert_number),
            repository=event.repository.full_name,
            title=title,
            description=description,
            github_url=event.url,
            state=event.state,
            pr_number=pr_number,
            label_name=_SECURITY_LABEL_NAME,
        )

    async def handle_linear_issue_done(self, trigger: ReviewTrigger) -> None:
        """React to a synced Linear issue landing on a completed-type state.

        The reverse of `close_when_closed` in `_upsert`: closes the linked
        GitHub Issue to match. Called from the Linear webhook the same way
        `AutoMergeService.handle_issue_done` is (same trigger, same "issue
        landed on Done" detection) — but that path is keyed off a `ReviewJob`
        (AIDA-MATE's own code reviews); this one is keyed off a `SyncMapping`
        (a GitHub Issue AIDA-MATE mirrored into Linear), a completely
        different relationship. A Linear issue with no `SyncMapping`, or one
        for a security alert rather than a plain Issue, is a normal no-op —
        most Linear Done transitions have nothing to do with this sync at all.

        Never raises: called directly from the webhook handler, which must
        always return a 2xx to Linear regardless of what happens here.
        """
        mapping = await self._mappings.find_by_linear_issue_id(trigger.issue_id)
        if mapping is None or mapping.source_type != "issue":
            return

        try:
            owner, name = mapping.repository.split("/", 1)
        except ValueError:
            logger.warning(
                "Malformed repository on sync mapping; cannot close",
                extra={"linear_issue_id": trigger.issue_id, "repository": mapping.repository},
            )
            return

        try:
            await self._github.close_issue(RepositoryRef(owner=owner, name=name), int(mapping.source_id))
        except GitHubError as exc:
            logger.warning(
                "Could not close the linked GitHub issue",
                extra={
                    "linear_issue_id": trigger.issue_id,
                    "repository": mapping.repository,
                    "error": str(exc),
                },
            )
            return

        logger.info(
            "Closed linked GitHub issue to match Linear Done",
            extra={"linear_issue_id": trigger.issue_id, "repository": mapping.repository},
        )

    async def _find_pr_referencing_issue(self, event: GitHubIssueEvent) -> int | None:
        """Best-effort: search PRs (open or closed) mentioning "#<number>".

        Same documented limitation as `pr_resolver.py`'s own `TitleBodyStrategy`
        — a mention is not always a claim of fixing it — not a new risk class.
        """
        try:
            items = await self._github.search_pull_requests_referencing(
                event.repository, f"#{event.number}"
            )
        except GitHubError as exc:
            logger.warning(
                "Could not search for a related PR", extra={"github_issue": event.number, "error": str(exc)}
            )
            return None
        return items[0].get("number") if items else None

    async def _find_pr_for_commit(self, event: SecurityAlertEvent) -> int | None:
        """The reliable relationship signal for alerts: the commit they were found on."""
        try:
            items = await self._github.list_pull_requests_for_commit(event.repository, event.commit_sha)
        except GitHubError as exc:
            logger.warning(
                "Could not resolve the PR for this alert's commit",
                extra={"alert_number": event.alert_number, "error": str(exc)},
            )
            return None
        return items[0].get("number") if items else None

    async def _upsert(
        self,
        *,
        fingerprint: str,
        source_type: str,
        source_id: str,
        repository: str,
        title: str,
        description: str,
        github_url: str,
        state: str,
        pr_number: int | None,
        label_name: str,
        close_when_closed: bool = False,
    ) -> None:
        """Create or update the Linear issue for `fingerprint`.

        `close_when_closed`: when true and `state == "closed"`, the Linear
        issue is moved to the team's completed-type state immediately after
        being created/updated — see the module docstring. False for security
        alerts, which don't set this.

        Never raises: called directly from the GitHub webhook handler. A
        `LinearError` anywhere in this method (team lookup, create, update) is
        caught and logged rather than propagated, same fix pattern
        `GitHubMergeSyncService`'s live test proved necessary.

        Label resolution is handled separately and does NOT abort the sync on
        failure — degrades to creating an untagged issue instead. This was
        originally an all-or-nothing failure (label error blocks the whole
        sync), but live testing found Linear can reject `issueLabelCreate`
        for the OAuth app actor with `FORBIDDEN: "not allowed to take action"`
        even though `issueCreate` succeeds — a workspace permission
        restriction independent of anything this code controls. Since that
        would otherwise silently block 100% of syncs whenever the label
        doesn't already exist, the issue's content (the part with real
        information) is worth creating even when the tag can't be. Once a
        human pre-creates the label once in Linear's UI, `ensure_label_id`'s
        existing-label lookup finds it and every subsequent sync gets tagged
        normally — no code change needed for that half.

        Known, accepted race: `SyncMapping` creation happens *after* the
        Linear issue is created, not before (unlike `ReviewJob`, which
        reserves its idempotency key before any side-effecting work). Two
        genuinely concurrent deliveries for the same brand-new GitHub object
        could each create a Linear issue before either's mapping is stored,
        leaving one orphaned. Deliberately not solved with a reserve-first
        two-phase write: that trades a rare, logged, human-visible duplicate
        for a worse failure mode — a permanently stuck mapping if the
        Linear call fails after reserving but before finishing. If this
        proves to be a real problem in practice, `SyncMapping` needs a status
        field first, not just a reordering of these calls.
        """
        try:
            existing = await self._mappings.find_by_fingerprint(fingerprint)
            if existing is not None and existing.linear_issue_id:
                await self._linear.update_issue_content(
                    existing.linear_issue_id, title=title, description=description
                )
                existing.state = state
                existing.pr_number = pr_number
                existing.touch()
                await self._mappings.save(existing)
                logger.info(
                    "Updated synced Linear issue",
                    extra={"fingerprint": fingerprint, "linear_issue_id": existing.linear_issue_id},
                )
                if close_when_closed and state.lower() == "closed":
                    await self._close_in_linear(existing.linear_issue_id, fingerprint=fingerprint)
                return

            team_id = await self._linear.find_team_id_by_key(self._team_key)
            if team_id is None:
                logger.warning(
                    "Linear team not found; cannot sync",
                    extra={"team_key": self._team_key, "fingerprint": fingerprint},
                )
                return

            label_id: str | None = None
            try:
                label_id = await self._linear.ensure_label_id(team_id, label_name)
            except LinearError as exc:
                logger.warning(
                    "Could not resolve/create the Linear label; creating the issue untagged",
                    extra={"fingerprint": fingerprint, "label_name": label_name, "error": str(exc)},
                )

            issue_id, identifier = await self._linear.create_issue(
                team_id, title, description, label_ids=[label_id] if label_id else None
            )
            mapping = SyncMapping(
                fingerprint=fingerprint,
                source_type=source_type,
                source_id=source_id,
                repository=repository,
                linear_issue_id=issue_id,
                pr_number=pr_number,
                github_url=github_url,
                state=state,
            )
            stored, created = await self._mappings.create(mapping)
            if not created:
                logger.warning(
                    "Lost a race creating this mapping; a duplicate Linear issue may exist",
                    extra={
                        "fingerprint": fingerprint,
                        "created_linear_issue_id": issue_id,
                        "winning_linear_issue_id": stored.linear_issue_id,
                    },
                )
                return
            logger.info(
                "Created synced Linear issue",
                extra={"fingerprint": fingerprint, "linear_identifier": identifier},
            )
            if self._default_schedule_service is not None:
                await self._default_schedule_service.ensure_for_repository(repository)
            if close_when_closed and state.lower() == "closed":
                await self._close_in_linear(issue_id, fingerprint=fingerprint)
        except LinearError as exc:
            logger.warning(
                "Could not sync GitHub object to Linear",
                extra={"fingerprint": fingerprint, "error": str(exc)},
            )

    async def _close_in_linear(self, linear_issue_id: str, *, fingerprint: str) -> None:
        """Move a synced Linear issue to its team's completed-type state.

        Resolves the team via `self._team_key` rather than looking the issue
        up first — unlike `GitHubMergeSyncService` (§1b), which syncs
        arbitrary pre-existing Linear issues and so must discover their team,
        every issue this service creates already lives on the one configured
        `team_key`. Called from inside `_upsert`'s `try/except LinearError`,
        so a failure here is caught by the same handler — logged, not raised.
        """
        team_id = await self._linear.find_team_id_by_key(self._team_key)
        if team_id is None:
            logger.warning(
                "Linear team not found; cannot close synced issue",
                extra={"team_key": self._team_key, "linear_issue_id": linear_issue_id},
            )
            return

        done_state_id = await self._linear.find_done_state_id(team_id)
        if done_state_id is None:
            logger.warning(
                "No completed-type workflow state found for this team; cannot close synced issue",
                extra={"team_key": self._team_key, "linear_issue_id": linear_issue_id},
            )
            return

        await self._linear.update_issue_state(linear_issue_id, done_state_id)
        logger.info(
            "Closed synced Linear issue to match closed GitHub issue",
            extra={"fingerprint": fingerprint, "linear_issue_id": linear_issue_id},
        )
