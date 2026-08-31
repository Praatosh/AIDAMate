"""GitHub REST client: auth, reads, pagination, and error translation."""

import httpx
import pytest
import respx

from app.core.errors import GitHubError, GitHubRateLimitError
from app.models.github import FileChangeStatus, PullRequestRef, RepositoryRef
from app.services.github_service import (
    GitHubService,
    StaticTokenCredentials,
    _normalize_private_key,
)

API = "https://api.github.com"
REPO = RepositoryRef(owner="acme", name="api")
REF = PullRequestRef(repository=REPO, number=431, url="https://github.com/acme/api/pull/431")


@pytest.fixture
def service() -> GitHubService:
    return GitHubService(StaticTokenCredentials("test-token"), httpx.AsyncClient())


def _pr_payload(**overrides) -> dict:
    payload = {
        "number": 431,
        "title": "Add OAuth authentication",
        "body": "Implements Google OAuth",
        "state": "open",
        "draft": False,
        "user": {"login": "karthigai"},
        "head": {"ref": "feature/mate-123-oauth", "sha": "3f2c9ab", "repo": {"full_name": "acme/api"}},
        "base": {"ref": "main"},
    }
    payload.update(overrides)
    return payload


# --- Reads ------------------------------------------------------------------


@respx.mock
async def test_get_pull_request(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431").mock(return_value=httpx.Response(200, json=_pr_payload()))
    respx.get(f"{API}/repos/acme/api/pulls/431/files").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(return_value=httpx.Response(200, json=[]))

    pull = await service.get_pull_request(REF)

    assert pull.title == "Add OAuth authentication"
    assert pull.head_sha == "3f2c9ab"
    assert pull.head_ref == "feature/mate-123-oauth"
    assert pull.base_ref == "main"
    assert pull.is_fork is False


@respx.mock
async def test_fork_is_detected(service: GitHubService) -> None:
    """A fork head means the change is less trusted."""
    forked = _pr_payload(head={"ref": "patch", "sha": "abc", "repo": {"full_name": "outsider/api"}})
    respx.get(f"{API}/repos/acme/api/pulls/431").mock(return_value=httpx.Response(200, json=forked))
    respx.get(f"{API}/repos/acme/api/pulls/431/files").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(return_value=httpx.Response(200, json=[]))

    assert (await service.get_pull_request(REF)).is_fork is True


@respx.mock
async def test_get_changed_files(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "filename": "app/auth/middleware.py",
                    "status": "modified",
                    "additions": 12,
                    "deletions": 3,
                    "patch": "@@ -1 +1 @@",
                }
            ],
        )
    )

    files = await service.get_pull_request_files(REF)

    assert files[0].filename == "app/auth/middleware.py"
    assert files[0].status is FileChangeStatus.MODIFIED
    assert files[0].additions == 12


@respx.mock
async def test_unknown_file_status_is_tolerated(service: GitHubService) -> None:
    """An unrecognised status must not discard the file — its path still matters."""
    respx.get(f"{API}/repos/acme/api/pulls/431/files").mock(
        return_value=httpx.Response(200, json=[{"filename": "a.py", "status": "resurrected"}])
    )

    files = await service.get_pull_request_files(REF)

    assert len(files) == 1
    assert files[0].status is FileChangeStatus.CHANGED


@respx.mock
async def test_get_commits(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        return_value=httpx.Response(
            200, json=[{"sha": "abc123", "commit": {"message": "Add OAuth", "author": {"name": "K"}}}]
        )
    )

    commits = await service.get_pull_request_commits(REF)

    assert commits[0].sha == "abc123"
    assert commits[0].message == "Add OAuth"


@respx.mock
async def test_get_diff_requests_the_diff_media_type(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431").mock(
        return_value=httpx.Response(200, text="diff --git a/x b/x")
    )

    diff = await service.get_pull_request_diff(REF)

    assert diff.startswith("diff --git")
    assert route.calls.last.request.headers["accept"] == "application/vnd.github.v3.diff"


# --- Pagination -------------------------------------------------------------


@respx.mock
async def test_pagination_follows_full_pages(service: GitHubService) -> None:
    full_page = [{"filename": f"file{i}.py", "status": "modified"} for i in range(100)]
    respx.get(f"{API}/repos/acme/api/pulls/431/files").mock(
        side_effect=[
            httpx.Response(200, json=full_page),
            httpx.Response(200, json=[{"filename": "last.py", "status": "added"}]),
        ]
    )

    files = await service.get_pull_request_files(REF)

    assert len(files) == 101


@respx.mock
async def test_pagination_stops_on_a_short_page(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431/files").mock(
        return_value=httpx.Response(200, json=[{"filename": "a.py", "status": "added"}])
    )

    await service.get_pull_request_files(REF)

    assert route.call_count == 1


# --- Auth -------------------------------------------------------------------


@respx.mock
async def test_token_is_sent_as_bearer(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        return_value=httpx.Response(200, json=[])
    )

    await service.get_pull_request_commits(REF)

    assert route.calls.last.request.headers["authorization"] == "Bearer test-token"
    assert route.calls.last.request.headers["x-github-api-version"] == "2022-11-28"


# --- Error translation ------------------------------------------------------


@respx.mock
async def test_rate_limit_is_distinguished_from_forbidden(service: GitHubService) -> None:
    """Only an exhausted quota is retryable; a plain 403 is not."""
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})
    )

    with pytest.raises(GitHubRateLimitError):
        await service.get_pull_request_commits(REF)


@respx.mock
async def test_plain_forbidden_is_a_generic_error(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "4999"}, json={})
    )

    with pytest.raises(GitHubError) as exc_info:
        await service.get_pull_request_commits(REF)

    assert not isinstance(exc_info.value, GitHubRateLimitError)


@respx.mock
async def test_not_found_is_reported_clearly(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(return_value=httpx.Response(404))

    with pytest.raises(GitHubError, match="not found"):
        await service.get_pull_request_commits(REF)


@respx.mock
async def test_transport_failure_is_translated(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(GitHubError):
        await service.get_pull_request_commits(REF)


# --- Retry (app/core/retry.py) ----------------------------------------------


@respx.mock
async def test_a_server_error_is_retried_then_succeeds(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=[])]
    )

    result = await service.get_pull_request_commits(REF)

    assert result == []
    assert route.call_count == 2


@respx.mock
async def test_a_rate_limit_is_retried_then_succeeds(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        side_effect=[
            httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={}),
            httpx.Response(200, json=[]),
        ]
    )

    result = await service.get_pull_request_commits(REF)

    assert result == []
    assert route.call_count == 2


@respx.mock
async def test_a_transport_failure_is_retried_then_succeeds(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=[])]
    )

    result = await service.get_pull_request_commits(REF)

    assert result == []
    assert route.call_count == 2


@respx.mock
async def test_a_404_is_not_retried(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(return_value=httpx.Response(404))

    with pytest.raises(GitHubError):
        await service.get_pull_request_commits(REF)

    assert route.call_count == 1


@respx.mock
async def test_a_plain_403_is_not_retried(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "4999"}, json={})
    )

    with pytest.raises(GitHubError):
        await service.get_pull_request_commits(REF)

    assert route.call_count == 1


@respx.mock
async def test_download_archive_retries_a_server_error_then_succeeds(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/tarball/abc123").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, content=b"archive-bytes")]
    )

    result = await service.download_archive(REPO, "abc123")

    assert result == b"archive-bytes"
    assert route.call_count == 2


@respx.mock
async def test_errors_do_not_echo_the_token(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431/commits").mock(
        return_value=httpx.Response(401, text="test-token is invalid")
    )

    with pytest.raises(GitHubError) as exc_info:
        await service.get_pull_request_commits(REF)

    assert "test-token" not in str(exc_info.value)


# --- Search / listing -------------------------------------------------------


@respx.mock
async def test_search_builds_a_scoped_query(service: GitHubService) -> None:
    route = respx.get(f"{API}/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))

    await service.search_pull_requests(REPO, "MATE-123")

    query = route.calls.last.request.url.params["q"]
    assert "repo:acme/api" in query
    assert "is:pr" in query
    assert "MATE-123" in query


@respx.mock
async def test_list_open_pull_requests(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls").mock(
        return_value=httpx.Response(200, json=[{"number": 1}])
    )

    pulls = await service.list_open_pull_requests(REPO)

    assert pulls == [{"number": 1}]
    assert route.calls.last.request.url.params["state"] == "open"


@respx.mock
async def test_list_pull_requests_for_commit(service: GitHubService) -> None:
    """The relationship-detection mechanism for security alerts (CLAUDE.md §1c)."""
    respx.get(f"{API}/repos/acme/api/commits/eea1654/pulls").mock(
        return_value=httpx.Response(200, json=[{"number": 5, "title": "Frontend cleanup"}])
    )

    pulls = await service.list_pull_requests_for_commit(REPO, "eea1654")

    assert pulls == [{"number": 5, "title": "Frontend cleanup"}]


@respx.mock
async def test_search_pull_requests_referencing_is_not_open_restricted(service: GitHubService) -> None:
    """Unlike `search_pull_requests` (pr_resolver.py's is:open-only strategy), a GitHub
    Issue's linking PR may already be merged/closed — this search must not exclude it."""
    route = respx.get(f"{API}/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))

    await service.search_pull_requests_referencing(REPO, "#42")

    query = route.calls.last.request.url.params["q"]
    assert "repo:acme/api" in query
    assert "is:pr" in query
    assert "is:open" not in query
    assert "#42" in query


@respx.mock
async def test_search_strips_embedded_quotes_from_the_search_text(service: GitHubService) -> None:
    """Security-audit finding: `text` was previously embedded directly inside
    a `"..."` quoted phrase with no escaping. GitHub's search syntax has no
    way to escape a literal `"`, so an embedded one would close the phrase
    early and let the rest of `text` be interpreted as extra search
    qualifiers (e.g. appending `author:` or `repo:` terms)."""
    route = respx.get(f"{API}/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))

    await service.search_pull_requests(REPO, 'x" repo:someone-elses/private-repo "')

    query = route.calls.last.request.url.params["q"]
    # Exactly the two delimiter quotes wrapping the phrase should remain —
    # none of the ones embedded in `text` should have survived.
    assert query.count('"') == 2


@respx.mock
async def test_search_referencing_strips_embedded_quotes_too(service: GitHubService) -> None:
    route = respx.get(f"{API}/search/issues").mock(return_value=httpx.Response(200, json={"items": []}))

    await service.search_pull_requests_referencing(REPO, 'x" repo:someone-elses/private-repo "')

    query = route.calls.last.request.url.params["q"]
    # Exactly the two delimiter quotes wrapping the phrase should remain —
    # none of the ones embedded in `text` should have survived.
    assert query.count('"') == 2


# --- Scheduled prompts (CLAUDE.md §1d) --------------------------------------


@respx.mock
async def test_get_default_branch(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api").mock(
        return_value=httpx.Response(200, json={"default_branch": "develop"})
    )

    assert await service.get_default_branch(REPO) == "develop"


@respx.mock
async def test_get_default_branch_falls_back_to_main_when_missing(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api").mock(return_value=httpx.Response(200, json={}))

    assert await service.get_default_branch(REPO) == "main"


@respx.mock
async def test_get_commit_sha_resolves_a_branch_name(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/commits/develop").mock(
        return_value=httpx.Response(200, json={"sha": "deadbeef"})
    )

    sha = await service.get_commit_sha(REPO, "develop")

    assert sha == "deadbeef"
    assert route.called


@respx.mock
async def test_get_commit_sha_raises_when_the_response_has_no_sha(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/commits/develop").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(GitHubError, match="no commit sha"):
        await service.get_commit_sha(REPO, "develop")


@respx.mock
async def test_get_pull_request_head_sha(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/pulls/431").mock(
        return_value=httpx.Response(200, json=_pr_payload())
    )

    sha = await service.get_pull_request_head_sha(REF)

    assert sha == "3f2c9ab"
    assert route.called
    # Deliberately lighter than get_pull_request: no /files or /commits calls.
    assert route.call_count == 1


@respx.mock
async def test_get_pull_request_head_sha_raises_when_the_response_has_no_sha(
    service: GitHubService,
) -> None:
    respx.get(f"{API}/repos/acme/api/pulls/431").mock(
        return_value=httpx.Response(200, json=_pr_payload(head={}))
    )

    with pytest.raises(GitHubError, match="no head sha"):
        await service.get_pull_request_head_sha(REF)


# --- Private key handling ---------------------------------------------------


def test_pem_with_escaped_newlines_is_restored() -> None:
    """`.env` files cannot hold real newlines, so `\\n` is the usual workaround."""
    normalized = _normalize_private_key(
        "-----BEGIN RSA PRIVATE KEY-----\\nabc\\n-----END RSA PRIVATE KEY-----"
    )

    assert "\n" in normalized
    assert "\\n" not in normalized


def test_base64_encoded_pem_is_decoded() -> None:
    import base64

    pem = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    encoded = base64.b64encode(pem.encode()).decode()

    assert _normalize_private_key(encoded) == pem


def test_garbage_private_key_is_rejected() -> None:
    with pytest.raises(GitHubError):
        _normalize_private_key("!!! not a key !!!")


def test_base64_of_non_pem_is_rejected() -> None:
    import base64

    with pytest.raises(GitHubError, match="not a PEM"):
        _normalize_private_key(base64.b64encode(b"hello world").decode())
