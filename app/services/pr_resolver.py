"""Finding the GitHub pull request a Linear issue refers to.

Three strategies, tried in descending order of confidence. The first to
produce a match wins; resolution stops there rather than gathering candidates,
because a lower-confidence strategy agreeing adds nothing and disagreeing
would need an arbitrary tie-break.

1. **Attachment** — Linear's own GitHub integration attaches the PR to the
   issue. This is a real link, not an inference, so it is always preferred.
2. **Branch name** — a branch such as `feature/mate-123-oauth` contains the
   ticket identifier. Strong, and the common convention when the native
   integration is not installed.
3. **Title or body** — the PR text mentions `MATE-123`. Weakest: a mention can
   be a reference rather than a claim of implementation.

Strategies 2 and 3 must search GitHub, so they only run against repositories
in `GITHUB_REPO_ALLOWLIST`. Without one they are skipped — scanning every
accessible repository would be slow and would invite false matches.

Resolution runs *before* any sandbox is provisioned, so an issue with no PR
costs nothing but two API calls.
"""

import re
from typing import Protocol

from app.core.errors import GitHubError, LinkedPullRequestNotFoundError
from app.core.events import PR_RESOLVED
from app.core.logging import get_logger
from app.models.github import PullRequestRef, RepositoryRef
from app.models.linear import LinearIssue

logger = get_logger(__name__)

#: `https://github.com/owner/repo/pull/123`, with optional trailing segments
#: such as `/files` — Linear attachments sometimes carry the deep link.
_PR_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


def parse_pull_request_url(url: str) -> PullRequestRef | None:
    """Parse a GitHub PR URL into a reference, or None if it is not one."""
    match = _PR_URL_PATTERN.match((url or "").strip())
    if match is None:
        return None

    return PullRequestRef(
        repository=RepositoryRef(owner=match.group("owner"), name=match.group("repo")),
        number=int(match.group("number")),
        url=(
            f"https://github.com/{match.group('owner')}/{match.group('repo')}"
            f"/pull/{match.group('number')}"
        ),
    )


class IResolutionStrategy(Protocol):
    """One way of locating a pull request for an issue."""

    name: str

    async def resolve(self, issue: LinearIssue) -> PullRequestRef | None: ...


class AttachmentStrategy:
    """Read the PR straight off Linear's own GitHub attachment."""

    name = "attachment"

    async def resolve(self, issue: LinearIssue) -> PullRequestRef | None:
        """Return the most recently attached GitHub PR, if any.

        Matches on URL shape rather than `sourceType`: the URL is
        unambiguous, while the source field varies and is sometimes absent.
        Later attachments win, so a superseding PR takes precedence.
        """
        matches = [
            ref
            for attachment in issue.attachments
            if (ref := parse_pull_request_url(attachment.url)) is not None
        ]
        if not matches:
            return None

        if len(matches) > 1:
            logger.info(
                "Issue has multiple attached pull requests; using the most recent",
                extra={"linear_issue_id": issue.id, "candidates": [ref.slug for ref in matches]},
            )
        return matches[-1]


class BranchNameStrategy:
    """Match the ticket identifier against open PR branch names."""

    name = "branch_name"

    def __init__(self, github, repositories: list[RepositoryRef]) -> None:
        self._github = github
        self._repositories = repositories

    async def resolve(self, issue: LinearIssue) -> PullRequestRef | None:
        """Find an open PR whose head branch contains the ticket identifier."""
        identifier = (issue.identifier or "").strip().lower()
        if not identifier or not self._repositories:
            return None

        for repository in self._repositories:
            try:
                pulls = await self._github.list_open_pull_requests(repository)
            except GitHubError as exc:
                # One unreachable repository must not abort the whole search.
                logger.warning(
                    "Could not list pull requests while resolving",
                    extra={"repository": repository.full_name, "error": str(exc)},
                )
                continue

            for pull in pulls:
                branch = ((pull.get("head") or {}).get("ref") or "").lower()
                if identifier in branch:
                    return _ref_from_payload(repository, pull)
        return None


class TitleBodyStrategy:
    """Match the ticket identifier inside open PR titles and descriptions."""

    name = "title_body"

    def __init__(self, github, repositories: list[RepositoryRef]) -> None:
        self._github = github
        self._repositories = repositories

    async def resolve(self, issue: LinearIssue) -> PullRequestRef | None:
        """Find an open PR whose title or body mentions the ticket identifier."""
        identifier = (issue.identifier or "").strip()
        if not identifier or not self._repositories:
            return None

        for repository in self._repositories:
            try:
                items = await self._github.search_pull_requests(repository, identifier)
            except GitHubError as exc:
                logger.warning(
                    "Could not search pull requests while resolving",
                    extra={"repository": repository.full_name, "error": str(exc)},
                )
                continue

            for item in items:
                # GitHub's search index lags, and matching is fuzzy, so the
                # identifier is re-checked literally before trusting the hit.
                haystack = f"{item.get('title') or ''} {item.get('body') or ''}".lower()
                if identifier.lower() not in haystack:
                    continue

                if (ref := parse_pull_request_url(item.get("html_url") or "")) is not None:
                    return ref
        return None


def _ref_from_payload(repository: RepositoryRef, pull: dict) -> PullRequestRef:
    """Build a reference from a GitHub pull-request payload."""
    number = int(pull.get("number", 0))
    return PullRequestRef(
        repository=repository,
        number=number,
        url=pull.get("html_url") or f"https://github.com/{repository.full_name}/pull/{number}",
    )


class PullRequestResolver:
    """Runs the resolution strategies in order of confidence."""

    def __init__(self, strategies: list[IResolutionStrategy]) -> None:
        self._strategies = strategies

    async def resolve(self, issue: LinearIssue) -> PullRequestRef:
        """Locate the pull request for `issue`.

        Raises:
            LinkedPullRequestNotFoundError: if no strategy matches. The caller
                reports this to the requester; no sandbox is provisioned.
        """
        for strategy in self._strategies:
            found = await strategy.resolve(issue)
            if found is None:
                continue

            logger.info(
                PR_RESOLVED,
                extra={
                    "event": PR_RESOLVED,
                    "linear_issue_id": issue.id,
                    "linear_issue_identifier": issue.identifier,
                    "github_pr": found.slug,
                    "strategy": strategy.name,
                },
            )
            return found

        logger.info(
            "No pull request could be resolved",
            extra={
                "linear_issue_id": issue.id,
                "linear_issue_identifier": issue.identifier,
                "strategies_tried": [s.name for s in self._strategies],
            },
        )
        raise LinkedPullRequestNotFoundError(
            f"No GitHub pull request found for Linear issue {issue.identifier or issue.id}"
        )


def build_resolver(github, repositories: list[RepositoryRef]) -> PullRequestResolver:
    """Assemble the standard strategy chain.

    The GitHub-searching strategies are omitted entirely when no repositories
    are allowlisted, so the chain never contains a step that cannot succeed.
    """
    strategies: list[IResolutionStrategy] = [AttachmentStrategy()]
    if repositories:
        strategies.append(BranchNameStrategy(github, repositories))
        strategies.append(TitleBodyStrategy(github, repositories))
    return PullRequestResolver(strategies)
