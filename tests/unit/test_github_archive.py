"""GitHubService.download_archive: redirect handling and error translation.

`GET /repos/{owner}/{repo}/tarball/{sha}` 302s to a signed download host.
httpx defaults to NOT following redirects (confirmed by reading its source,
not assumed) — these tests exist specifically to prove `download_archive`
overrides that default and actually follows the chain.
"""

import httpx
import pytest
import respx

from app.core.errors import GitHubError, GitHubUnavailableError, InvalidPullRequestError
from app.models.github import RepositoryRef
from app.services.github_service import GitHubService, StaticTokenCredentials

API = "https://api.github.com"
CODELOAD = "https://codeload.github.com/acme/api/tar.gz/3f2c9ab"
REPO = RepositoryRef(owner="acme", name="api")


@pytest.fixture
def service() -> GitHubService:
    return GitHubService(StaticTokenCredentials("test-token"), httpx.AsyncClient())


@respx.mock
async def test_follows_the_redirect_to_the_final_bytes(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD})
    )
    respx.get(CODELOAD).mock(return_value=httpx.Response(200, content=b"fake-tarball-bytes"))

    result = await service.download_archive(REPO, "3f2c9ab")

    assert result == b"fake-tarball-bytes"


@respx.mock
async def test_a_plain_200_without_a_redirect_also_works(service: GitHubService) -> None:
    """Not every environment necessarily redirects; a direct 200 must work too."""
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(
        return_value=httpx.Response(200, content=b"direct-bytes")
    )

    assert await service.download_archive(REPO, "3f2c9ab") == b"direct-bytes"


@respx.mock
async def test_404_maps_to_invalid_pull_request(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/tarball/deadsha").mock(return_value=httpx.Response(404))

    with pytest.raises(InvalidPullRequestError, match="deadsha"):
        await service.download_archive(REPO, "deadsha")


@respx.mock
async def test_other_4xx_5xx_maps_to_github_error(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(return_value=httpx.Response(500))

    with pytest.raises(GitHubError) as exc_info:
        await service.download_archive(REPO, "3f2c9ab")

    assert not isinstance(exc_info.value, InvalidPullRequestError)
    assert not isinstance(exc_info.value, GitHubUnavailableError)


@respx.mock
async def test_transport_failure_maps_to_github_unavailable(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(GitHubUnavailableError):
        await service.download_archive(REPO, "3f2c9ab")


@respx.mock
async def test_the_bearer_token_is_sent_on_the_initial_request(service: GitHubService) -> None:
    route = respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(
        return_value=httpx.Response(200, content=b"x")
    )

    await service.download_archive(REPO, "3f2c9ab")

    assert route.calls.last.request.headers["authorization"] == "Bearer test-token"


@respx.mock
async def test_cross_origin_redirect_does_not_forward_the_token(service: GitHubService) -> None:
    """httpx strips Authorization on a cross-host redirect by default — the
    GitHub token must never reach the third-party signed-URL host."""
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD})
    )
    codeload_route = respx.get(CODELOAD).mock(return_value=httpx.Response(200, content=b"x"))

    await service.download_archive(REPO, "3f2c9ab")

    assert "authorization" not in {k.lower() for k in codeload_route.calls.last.request.headers}


@respx.mock
async def test_result_is_raw_bytes_not_json_decoded(service: GitHubService) -> None:
    """A binary body must never be run through the JSON-decoding _get() path."""
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(
        return_value=httpx.Response(200, content=b"\x1f\x8b\x08\x00binary-gzip-header")
    )

    result = await service.download_archive(REPO, "3f2c9ab")

    assert isinstance(result, bytes)
    assert result.startswith(b"\x1f\x8b")


@respx.mock
async def test_an_oversized_archive_is_rejected(
    service: GitHubService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security-audit finding: this was previously unbounded — `response.content`
    buffered the entire body in memory regardless of size, unlike every other
    bounded read in this codebase. A PR whose head commit's tree contains a
    very large blob (achievable by any external contributor) must not be able
    to exhaust memory on a single review. The real limit is 500 MB; shrunk
    here so the test doesn't need to actually transfer that much."""
    monkeypatch.setattr("app.services.github_service._MAX_ARCHIVE_BYTES", 10)
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(
        return_value=httpx.Response(200, content=b"x" * 11)
    )

    with pytest.raises(GitHubError, match="exceeds"):
        await service.download_archive(REPO, "3f2c9ab")


@respx.mock
async def test_an_archive_right_at_the_limit_is_accepted(
    service: GitHubService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.github_service._MAX_ARCHIVE_BYTES", 10)
    respx.get(f"{API}/repos/acme/api/tarball/3f2c9ab").mock(
        return_value=httpx.Response(200, content=b"x" * 10)
    )

    result = await service.download_archive(REPO, "3f2c9ab")

    assert len(result) == 10
