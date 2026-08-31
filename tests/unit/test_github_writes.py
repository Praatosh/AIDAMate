"""GitHub write operations: label creation, exclusivity, and comment updates."""

import httpx
import pytest
import respx

from app.core.errors import GitHubError, GitHubUnavailableError, PullRequestNotMergeableError
from app.models.github import PullRequestRef, RepositoryRef
from app.services.github_service import GitHubService, StaticTokenCredentials

API = "https://api.github.com"
REPO = RepositoryRef(owner="acme", name="api")
REF = PullRequestRef(repository=REPO, number=431, url="https://github.com/acme/api/pull/431")


@pytest.fixture
def service() -> GitHubService:
    return GitHubService(StaticTokenCredentials("test-token"), httpx.AsyncClient())


# --- Label creation ----------------------------------------------------------


@respx.mock
async def test_creates_only_missing_labels(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "risk: high"}])
    )
    create = respx.post(f"{API}/repos/acme/api/labels").mock(return_value=httpx.Response(201, json={}))

    await service.ensure_labels_exist(REPO, {"risk: high": "d93f0b", "area: auth": "1d76db"})

    assert create.call_count == 1
    assert b"area: auth" in create.calls.last.request.content


@respx.mock
async def test_existing_labels_are_left_alone(service: GitHubService) -> None:
    """A maintainer may have deliberately recoloured a label."""
    respx.get(f"{API}/repos/acme/api/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "risk: high"}])
    )
    create = respx.post(f"{API}/repos/acme/api/labels")

    await service.ensure_labels_exist(REPO, {"risk: high": "000000"})

    assert create.call_count == 0


@respx.mock
async def test_label_creation_failure_is_not_fatal(service: GitHubService) -> None:
    """Losing one label must not lose the whole review."""
    respx.get(f"{API}/repos/acme/api/labels").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/acme/api/labels").mock(return_value=httpx.Response(422, json={}))

    await service.ensure_labels_exist(REPO, {"area: auth": "1d76db"})


# --- Applying labels ---------------------------------------------------------


@respx.mock
async def test_applies_only_missing_labels(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/issues/431/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "area: auth"}])
    )
    add = respx.post(f"{API}/repos/acme/api/issues/431/labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    await service.apply_labels(REF, {"area: auth", "risk: high"})

    assert b"risk: high" in add.calls.last.request.content
    assert b"area: auth" not in add.calls.last.request.content


@respx.mock
async def test_stale_exclusive_label_is_removed(service: GitHubService) -> None:
    """A re-reviewed PR must not carry both `risk: low` and `risk: high`."""
    respx.get(f"{API}/repos/acme/api/issues/431/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "risk: low"}])
    )
    delete = respx.delete(url__regex=rf"{API}/repos/acme/api/issues/431/labels/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{API}/repos/acme/api/issues/431/labels").mock(return_value=httpx.Response(200, json=[]))

    await service.apply_labels(REF, {"risk: high"}, exclusive_prefixes=("risk",))

    assert delete.call_count == 1
    assert "risk" in str(delete.calls.last.request.url)


@respx.mock
async def test_human_added_labels_are_never_removed(service: GitHubService) -> None:
    """Only AIDA-MATE's own namespaces are managed."""
    respx.get(f"{API}/repos/acme/api/issues/431/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "bug"}, {"name": "priority: p0"}])
    )
    delete = respx.delete(url__regex=rf"{API}/repos/acme/api/issues/431/labels/.*")
    respx.post(f"{API}/repos/acme/api/issues/431/labels").mock(return_value=httpx.Response(200, json=[]))

    await service.apply_labels(REF, {"risk: high"}, exclusive_prefixes=("risk",))

    assert delete.call_count == 0


@respx.mock
async def test_matching_exclusive_label_is_not_churned(service: GitHubService) -> None:
    """Re-reviewing to the same verdict should not delete and re-add."""
    respx.get(f"{API}/repos/acme/api/issues/431/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "risk: high"}])
    )
    delete = respx.delete(url__regex=rf"{API}/repos/acme/api/issues/431/labels/.*")
    add = respx.post(f"{API}/repos/acme/api/issues/431/labels")

    await service.apply_labels(REF, {"risk: high"}, exclusive_prefixes=("risk",))

    assert delete.call_count == 0
    assert add.call_count == 0


@respx.mock
async def test_label_names_with_spaces_are_url_encoded(service: GitHubService) -> None:
    """`risk: low` contains a space and a colon; both must be escaped."""
    respx.get(f"{API}/repos/acme/api/issues/431/labels").mock(
        return_value=httpx.Response(200, json=[{"name": "risk: low"}])
    )
    delete = respx.delete(url__regex=rf"{API}/repos/acme/api/issues/431/labels/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{API}/repos/acme/api/issues/431/labels").mock(return_value=httpx.Response(200, json=[]))

    await service.apply_labels(REF, {"risk: high"}, exclusive_prefixes=("risk",))

    url = str(delete.calls.last.request.url)
    assert " " not in url
    assert "%20" in url or "%3A" in url


# --- Comments ----------------------------------------------------------------


@respx.mock
async def test_posts_a_new_comment(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/issues/431/comments").mock(
        return_value=httpx.Response(200, json=[])
    )
    post = respx.post(f"{API}/repos/acme/api/issues/431/comments").mock(
        return_value=httpx.Response(201, json={"id": 99})
    )

    comment_id = await service.add_comment(REF, "hello", marker="<!-- aida-mate-review -->")

    assert comment_id == 99
    assert post.call_count == 1


@respx.mock
async def test_updates_its_own_comment_instead_of_duplicating(service: GitHubService) -> None:
    """Re-review must edit one comment, not pile up a new one per push."""
    respx.get(f"{API}/repos/acme/api/issues/431/comments").mock(
        return_value=httpx.Response(
            200, json=[{"id": 55, "body": "old <!-- aida-mate-review --> text"}]
        )
    )
    patch = respx.patch(f"{API}/repos/acme/api/issues/comments/55").mock(
        return_value=httpx.Response(200, json={"id": 55})
    )
    post = respx.post(f"{API}/repos/acme/api/issues/431/comments")

    comment_id = await service.add_comment(REF, "new", marker="<!-- aida-mate-review -->")

    assert comment_id == 55
    assert patch.call_count == 1
    assert post.call_count == 0


@respx.mock
async def test_other_peoples_comments_are_not_touched(service: GitHubService) -> None:
    respx.get(f"{API}/repos/acme/api/issues/431/comments").mock(
        return_value=httpx.Response(200, json=[{"id": 7, "body": "a human said something"}])
    )
    post = respx.post(f"{API}/repos/acme/api/issues/431/comments").mock(
        return_value=httpx.Response(201, json={"id": 8})
    )

    assert await service.add_comment(REF, "x", marker="<!-- aida-mate-review -->") == 8
    assert post.call_count == 1


@respx.mock
async def test_without_a_marker_it_always_posts(service: GitHubService) -> None:
    post = respx.post(f"{API}/repos/acme/api/issues/431/comments").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    await service.add_comment(REF, "x")

    assert post.call_count == 1


# --- Merging -------------------------------------------------------------


@respx.mock
async def test_merges_a_pull_request(service: GitHubService) -> None:
    put = respx.put(f"{API}/repos/acme/api/pulls/431/merge").mock(
        return_value=httpx.Response(200, json={"merged": True})
    )

    await service.merge_pull_request(REF)

    assert put.call_count == 1
    assert b'"merge_method":"merge"' in put.calls.last.request.content


@respx.mock
async def test_not_mergeable_raises_a_distinct_error(service: GitHubService) -> None:
    respx.put(f"{API}/repos/acme/api/pulls/431/merge").mock(return_value=httpx.Response(405, json={}))

    with pytest.raises(PullRequestNotMergeableError):
        await service.merge_pull_request(REF)


@respx.mock
async def test_merge_of_unknown_pr_raises_generic_error(service: GitHubService) -> None:
    respx.put(f"{API}/repos/acme/api/pulls/431/merge").mock(return_value=httpx.Response(404, json={}))

    with pytest.raises(GitHubError):
        await service.merge_pull_request(REF)


@respx.mock
async def test_merge_transport_failure_raises_unavailable(service: GitHubService) -> None:
    respx.put(f"{API}/repos/acme/api/pulls/431/merge").mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(GitHubUnavailableError):
        await service.merge_pull_request(REF)


# --- Closing an issue ----------------------------------------------------------


@respx.mock
async def test_closes_an_issue(service: GitHubService) -> None:
    patch = respx.patch(f"{API}/repos/acme/api/issues/42").mock(
        return_value=httpx.Response(200, json={"number": 42, "state": "closed"})
    )

    await service.close_issue(REPO, 42)

    assert patch.call_count == 1
    body = patch.calls.last.request.content
    assert b'"state":"closed"' in body
    assert b'"state_reason":"completed"' in body


@respx.mock
async def test_close_unknown_issue_raises(service: GitHubService) -> None:
    respx.patch(f"{API}/repos/acme/api/issues/42").mock(return_value=httpx.Response(404, json={}))

    with pytest.raises(GitHubError):
        await service.close_issue(REPO, 42)


# --- Permission errors -------------------------------------------------------


@respx.mock
async def test_permission_denied_gives_an_actionable_message(service: GitHubService) -> None:
    """"HTTP 403" is not actionable; "missing a permission" is."""
    respx.get(f"{API}/repos/acme/api/issues/431/labels").mock(
        return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "4999"}, json={})
    )

    with pytest.raises(GitHubError, match="permission"):
        await service.apply_labels(REF, {"risk: high"})
