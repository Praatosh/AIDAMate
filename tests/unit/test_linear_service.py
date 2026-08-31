"""LinearService.add_comment/delete_comment: the delete-link mechanics.

`add_comment`'s real GraphQL behavior was previously only exercised via
fakes in consumer tests (orchestrator, review_worker, scheduled_prompt_
service, auto_merge_service) — this file tests it directly against a
respx-mocked Linear API for the first time.
"""

import httpx
import pytest
import respx

from app.core.errors import LinearError
from app.models.posted_comment import PostedComment
from app.services.linear_service import LINEAR_GRAPHQL_URL, LinearGraphQLClient, LinearService
from app.services.posted_comment_repository import InMemoryPostedCommentRepository


class StubAuth:
    """Supplies a token without touching OAuth."""

    async def get_access_token(self, organization_id=None) -> str:
        return "token-1"


def _comment_create_response(comment_id: str = "comment-1") -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"commentCreate": {"success": True, "comment": {"id": comment_id, "url": "u"}}}},
    )


# --- add_comment: no delete-link configuration -> unchanged behavior -------


@respx.mock
async def test_add_comment_posts_the_plain_body_when_not_configured_for_delete_links() -> None:
    service = LinearService(LinearGraphQLClient(httpx.AsyncClient()), StubAuth())
    route = respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_comment_create_response())

    await service.add_comment("issue-1", "Hello world")

    body = route.calls.last.request.read().decode()
    assert "Hello world" in body
    assert "Delete this comment" not in body


@respx.mock
async def test_add_comment_returns_the_new_comments_id() -> None:
    service = LinearService(LinearGraphQLClient(httpx.AsyncClient()), StubAuth())
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_comment_create_response("comment-42"))

    comment_id = await service.add_comment("issue-1", "Hello world")

    assert comment_id == "comment-42"


# --- add_comment: delete-link configuration ---------------------------------


@respx.mock
async def test_add_comment_appends_a_delete_link_when_configured() -> None:
    repository = InMemoryPostedCommentRepository()
    service = LinearService(
        LinearGraphQLClient(httpx.AsyncClient()),
        StubAuth(),
        posted_comment_repository=repository,
        base_url="https://aida-mate.example.com",
    )
    route = respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_comment_create_response())

    await service.add_comment("issue-1", "Hello world")

    body = route.calls.last.request.read().decode()
    assert "Hello world" in body
    assert "Delete this comment" in body
    assert "https://aida-mate.example.com/comments/" in body
    assert "/delete" in body


@respx.mock
async def test_add_comment_persists_the_token_to_comment_id_mapping() -> None:
    repository = InMemoryPostedCommentRepository()
    service = LinearService(
        LinearGraphQLClient(httpx.AsyncClient()),
        StubAuth(),
        posted_comment_repository=repository,
        base_url="https://aida-mate.example.com",
    )
    route = respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_comment_create_response("comment-99"))

    await service.add_comment("issue-1", "Hello world", organization_id="org-1")

    body = route.calls.last.request.read().decode()
    token = body.split("/comments/")[1].split("/delete")[0]
    saved = await repository.get(token)
    assert saved is not None
    assert saved.linear_comment_id == "comment-99"
    assert saved.organization_id == "org-1"


@respx.mock
async def test_add_comment_survives_a_repository_save_failure() -> None:
    """The comment already posted successfully — a bookkeeping failure must
    not surface as if the comment post itself failed."""

    class FailingRepository:
        async def save(self, record: PostedComment) -> PostedComment:
            raise RuntimeError("disk full")

    service = LinearService(
        LinearGraphQLClient(httpx.AsyncClient()),
        StubAuth(),
        posted_comment_repository=FailingRepository(),
        base_url="https://aida-mate.example.com",
    )
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_comment_create_response())

    comment_id = await service.add_comment("issue-1", "Hello world")  # must not raise

    assert comment_id == "comment-1"


# --- delete_comment -----------------------------------------------------------


@respx.mock
async def test_delete_comment_calls_the_mutation() -> None:
    service = LinearService(LinearGraphQLClient(httpx.AsyncClient()), StubAuth())
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"commentDelete": {"success": True}}})
    )

    await service.delete_comment("comment-1")

    body = route.calls.last.request.read().decode()
    assert "commentDelete" in body
    assert "comment-1" in body


@respx.mock
async def test_delete_comment_surfaces_http_errors_as_linear_errors() -> None:
    service = LinearService(LinearGraphQLClient(httpx.AsyncClient()), StubAuth())
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=httpx.Response(403, text="forbidden"))

    with pytest.raises(LinearError):
        await service.delete_comment("comment-1")
