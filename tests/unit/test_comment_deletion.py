"""The comment-deletion confirmation dialog endpoints.

Same shape as `test_merge_confirmation_api.py`: GET only reads/renders,
POST does the actual work. The default `client` fixture's real
`LinearService` has no OAuth installation configured, so a real
`delete_comment` call raises `LinearError` before touching the network —
used directly to test the failure path without any mocking.
"""

import asyncio

from app.models.posted_comment import PostedComment


def _seeded(client, **overrides) -> PostedComment:
    record = PostedComment(id="token-1", linear_comment_id="comment-1", **overrides)
    asyncio.run(client.app.state.posted_comment_repository.save(record))
    return record


# --- GET -------------------------------------------------------------------


def test_get_unknown_token_shows_nothing_here(client) -> None:
    response = client.get("/comments/does-not-exist/delete")

    assert response.status_code == 200
    assert "Nothing here" in response.text


def test_get_known_token_shows_the_confirmation_dialog(client) -> None:
    _seeded(client)

    response = client.get("/comments/token-1/delete")

    assert response.status_code == 200
    assert "Delete this comment" in response.text
    assert '<form method="post" action="/comments/token-1/delete">' in response.text


def test_page_sets_a_no_referrer_policy(client) -> None:
    _seeded(client)

    response = client.get("/comments/token-1/delete")

    assert "name='referrer' content='no-referrer'" in response.text


# --- POST --------------------------------------------------------------------


def test_post_unknown_token_shows_nothing_here(client) -> None:
    response = client.post("/comments/does-not-exist/delete")

    assert response.status_code == 200
    assert "Nothing here" in response.text


def test_post_known_token_deletes_and_removes_the_record(client) -> None:
    _seeded(client)

    class FakeLinearService:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str | None]] = []

        async def delete_comment(self, comment_id, *, organization_id=None) -> None:
            self.deleted.append((comment_id, organization_id))

    fake = FakeLinearService()
    client.app.state.linear_service = fake

    response = client.post("/comments/token-1/delete")

    assert response.status_code == 200
    assert "Deleted" in response.text
    assert fake.deleted == [("comment-1", None)]
    assert asyncio.run(client.app.state.posted_comment_repository.get("token-1")) is None


def test_post_passes_the_records_organization_id(client) -> None:
    _seeded(client, organization_id="org-1")

    class FakeLinearService:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str | None]] = []

        async def delete_comment(self, comment_id, *, organization_id=None) -> None:
            self.deleted.append((comment_id, organization_id))

    fake = FakeLinearService()
    client.app.state.linear_service = fake

    client.post("/comments/token-1/delete")

    assert fake.deleted == [("comment-1", "org-1")]


def test_post_when_linear_delete_fails_shows_an_error_and_keeps_the_record(client) -> None:
    """The default test env's `linear_service` has no OAuth installation, so a
    real `delete_comment` call raises `LinearError` before any network call —
    exercised directly here, no mocking needed."""
    _seeded(client)
    assert client.app.state.linear_service is not None

    response = client.post("/comments/token-1/delete")

    assert response.status_code == 200
    assert "Could not delete" in response.text
    assert asyncio.run(client.app.state.posted_comment_repository.get("token-1")) is not None
