"""Pull-request resolution: URL parsing and the three matching strategies."""

import pytest

from app.core.errors import GitHubError, LinkedPullRequestNotFoundError
from app.models.github import RepositoryRef
from app.models.linear import LinearAttachment, LinearIssue
from app.services.pr_resolver import (
    AttachmentStrategy,
    BranchNameStrategy,
    PullRequestResolver,
    TitleBodyStrategy,
    build_resolver,
    parse_pull_request_url,
)

REPO = RepositoryRef(owner="acme", name="api")


def _issue(identifier: str = "MATE-123", urls: list[str] | None = None) -> LinearIssue:
    return LinearIssue(
        id="issue-1",
        identifier=identifier,
        title="Add OAuth authentication",
        attachments=[LinearAttachment(url=url) for url in (urls or [])],
    )


class FakeGitHub:
    """Serves canned pull-request payloads without touching the network."""

    def __init__(self, pulls: list[dict] | None = None, search: list[dict] | None = None) -> None:
        self._pulls = pulls or []
        self._search = search or []
        self.list_calls: list[str] = []
        self.search_calls: list[tuple[str, str]] = []
        self.raise_on_list = False
        self.raise_on_search = False

    async def list_open_pull_requests(self, repository, *, limit: int = 100):
        self.list_calls.append(repository.full_name)
        if self.raise_on_list:
            raise GitHubError("repo unreachable")
        return self._pulls

    async def search_pull_requests(self, repository, text: str):
        self.search_calls.append((repository.full_name, text))
        if self.raise_on_search:
            raise GitHubError("search unavailable")
        return self._search


# --- URL parsing ------------------------------------------------------------


def test_parses_a_pr_url() -> None:
    ref = parse_pull_request_url("https://github.com/acme/api/pull/431")

    assert ref is not None
    assert ref.repository.owner == "acme"
    assert ref.repository.name == "api"
    assert ref.number == 431


def test_parses_deep_links() -> None:
    """Linear attachments sometimes point at /files or /commits."""
    ref = parse_pull_request_url("https://github.com/acme/api/pull/431/files")

    assert ref is not None
    assert ref.number == 431
    # The canonical URL is reconstructed, not echoed.
    assert ref.url == "https://github.com/acme/api/pull/431"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/api/issues/431",
        "https://gitlab.com/acme/api/pull/431",
        "https://github.com/acme/api",
        "https://example.com/acme/api/pull/431",
        "not a url",
        "",
    ],
)
def test_rejects_non_pr_urls(url: str) -> None:
    assert parse_pull_request_url(url) is None


def test_url_matching_is_case_insensitive_on_host() -> None:
    assert parse_pull_request_url("HTTPS://GitHub.com/acme/api/pull/7") is not None


# --- Attachment strategy ----------------------------------------------------


async def test_attachment_strategy_finds_the_pr() -> None:
    issue = _issue(urls=["https://github.com/acme/api/pull/431"])

    ref = await AttachmentStrategy().resolve(issue)

    assert ref.slug == "acme/api#431"


async def test_attachment_strategy_ignores_non_github_attachments() -> None:
    issue = _issue(urls=["https://figma.com/file/abc", "https://github.com/acme/api/pull/9"])

    assert (await AttachmentStrategy().resolve(issue)).number == 9


async def test_attachment_strategy_prefers_the_most_recent() -> None:
    """A superseding PR attached later should win."""
    issue = _issue(urls=["https://github.com/acme/api/pull/1", "https://github.com/acme/api/pull/2"])

    assert (await AttachmentStrategy().resolve(issue)).number == 2


async def test_attachment_strategy_returns_none_without_attachments() -> None:
    assert await AttachmentStrategy().resolve(_issue()) is None


# --- Branch-name strategy ---------------------------------------------------


async def test_branch_strategy_matches_identifier_in_branch() -> None:
    github = FakeGitHub(
        pulls=[
            {"number": 12, "head": {"ref": "chore/unrelated"}, "html_url": "https://github.com/acme/api/pull/12"},
            {
                "number": 431,
                "head": {"ref": "feature/mate-123-oauth"},
                "html_url": "https://github.com/acme/api/pull/431",
            },
        ]
    )

    ref = await BranchNameStrategy(github, [REPO]).resolve(_issue("MATE-123"))

    assert ref.number == 431


async def test_branch_matching_is_case_insensitive() -> None:
    """Ticket IDs are uppercase; branch names conventionally are not."""
    github = FakeGitHub(
        pulls=[{"number": 5, "head": {"ref": "FIX/MATE-123"}, "html_url": "https://github.com/acme/api/pull/5"}]
    )

    assert (await BranchNameStrategy(github, [REPO]).resolve(_issue("mate-123"))).number == 5


async def test_branch_strategy_returns_none_without_a_match() -> None:
    github = FakeGitHub(pulls=[{"number": 1, "head": {"ref": "main"}}])

    assert await BranchNameStrategy(github, [REPO]).resolve(_issue()) is None


async def test_branch_strategy_skips_when_no_repos_allowlisted() -> None:
    github = FakeGitHub()

    assert await BranchNameStrategy(github, []).resolve(_issue()) is None
    assert github.list_calls == []


async def test_branch_strategy_survives_an_unreachable_repo() -> None:
    """One bad repository must not abort the search across the rest."""
    github = FakeGitHub()
    github.raise_on_list = True

    assert await BranchNameStrategy(github, [REPO]).resolve(_issue()) is None


async def test_branch_strategy_needs_an_identifier() -> None:
    github = FakeGitHub(pulls=[{"number": 1, "head": {"ref": "anything"}}])

    assert await BranchNameStrategy(github, [REPO]).resolve(_issue(identifier="")) is None


# --- Title/body strategy ----------------------------------------------------


async def test_title_strategy_matches_identifier_in_title() -> None:
    github = FakeGitHub(
        search=[{"title": "Implement MATE-123 OAuth", "body": "", "html_url": "https://github.com/acme/api/pull/77"}]
    )

    assert (await TitleBodyStrategy(github, [REPO]).resolve(_issue("MATE-123"))).number == 77


async def test_title_strategy_rechecks_the_match_literally() -> None:
    """GitHub search is fuzzy and its index lags, so hits are re-verified."""
    github = FakeGitHub(
        search=[{"title": "Unrelated work", "body": "nothing here", "html_url": "https://github.com/acme/api/pull/8"}]
    )

    assert await TitleBodyStrategy(github, [REPO]).resolve(_issue("MATE-123")) is None


async def test_title_strategy_matches_body_too() -> None:
    github = FakeGitHub(
        search=[{"title": "OAuth", "body": "Fixes MATE-123", "html_url": "https://github.com/acme/api/pull/9"}]
    )

    assert (await TitleBodyStrategy(github, [REPO]).resolve(_issue("MATE-123"))).number == 9


async def test_title_strategy_survives_search_failure() -> None:
    github = FakeGitHub()
    github.raise_on_search = True

    assert await TitleBodyStrategy(github, [REPO]).resolve(_issue()) is None


# --- Cascade ----------------------------------------------------------------


async def test_attachment_wins_over_branch_match() -> None:
    """The highest-confidence strategy short-circuits the rest."""
    github = FakeGitHub(
        pulls=[{"number": 999, "head": {"ref": "feature/mate-123"}, "html_url": "https://github.com/acme/api/pull/999"}]
    )
    resolver = build_resolver(github, [REPO])

    ref = await resolver.resolve(_issue(urls=["https://github.com/acme/api/pull/431"]))

    assert ref.number == 431
    assert github.list_calls == []  # never consulted


async def test_falls_through_to_branch_then_title() -> None:
    github = FakeGitHub(
        pulls=[],
        search=[{"title": "MATE-123 work", "body": "", "html_url": "https://github.com/acme/api/pull/50"}],
    )

    ref = await build_resolver(github, [REPO]).resolve(_issue("MATE-123"))

    assert ref.number == 50
    assert github.list_calls == ["acme/api"]
    assert github.search_calls == [("acme/api", "MATE-123")]


async def test_no_match_raises_a_reportable_error() -> None:
    """The requester must be told, and no sandbox may be provisioned."""
    with pytest.raises(LinkedPullRequestNotFoundError) as exc_info:
        await build_resolver(FakeGitHub(), [REPO]).resolve(_issue())

    assert "Link a PR" in exc_info.value.user_message


async def test_resolver_without_repos_uses_attachments_only() -> None:
    resolver = build_resolver(FakeGitHub(), [])

    assert len(resolver._strategies) == 1

    with pytest.raises(LinkedPullRequestNotFoundError):
        await resolver.resolve(_issue())


async def test_empty_strategy_chain_raises() -> None:
    with pytest.raises(LinkedPullRequestNotFoundError):
        await PullRequestResolver([]).resolve(_issue())
