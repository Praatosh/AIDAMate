"""Team resolution, issue creation, and content updates (CLAUDE.md §1c)."""

import httpx
import pytest
import respx

from app.core.errors import LinearError
from app.services.linear_service import LINEAR_GRAPHQL_URL, LinearGraphQLClient, LinearService


class StubAuth:
    """Supplies a token without touching OAuth."""

    def __init__(self, token: str = "token-1") -> None:
        self._token = token

    async def get_access_token(self, organization_id=None) -> str:
        return self._token


@pytest.fixture
def service() -> LinearService:
    return LinearService(LinearGraphQLClient(httpx.AsyncClient()), StubAuth())


# --- find_team_id_by_key -------------------------------------------------------


@respx.mock
async def test_finds_the_team_by_key(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "teams": {
                        "nodes": [
                            {"id": "team-eng", "key": "ENG", "name": "Engineering"},
                            {"id": "team-git", "key": "GIT", "name": "GitHub-Test1"},
                        ]
                    }
                }
            },
        )
    )

    assert await service.find_team_id_by_key("GIT") == "team-git"


@respx.mock
async def test_team_key_match_is_case_insensitive(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"teams": {"nodes": [{"id": "team-git", "key": "GIT", "name": "x"}]}}}
        )
    )

    assert await service.find_team_id_by_key("git") == "team-git"


@respx.mock
async def test_returns_none_when_team_key_not_found(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"teams": {"nodes": []}}})
    )

    assert await service.find_team_id_by_key("NOPE") is None


# --- ensure_label_id -------------------------------------------------------------


@respx.mock
async def test_finds_an_existing_label_by_name(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"issueLabels": {"nodes": [{"id": "label-security", "name": "Security"}]}}},
        )
    )

    label_id = await service.ensure_label_id("team-git", "Security")

    assert label_id == "label-security"
    # Only one call — no create mutation when the label already exists.
    assert route.call_count == 1


@respx.mock
async def test_label_name_match_is_case_insensitive(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"issueLabels": {"nodes": [{"id": "label-security", "name": "security"}]}}},
        )
    )

    assert await service.ensure_label_id("team-git", "Security") == "label-security"


@respx.mock
async def test_creates_the_label_when_it_does_not_exist(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"issueLabels": {"nodes": []}}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "issueLabelCreate": {
                            "success": True,
                            "issueLabel": {"id": "label-new", "name": "Security"},
                        }
                    }
                },
            ),
        ]
    )

    label_id = await service.ensure_label_id("team-git", "Security")

    assert label_id == "label-new"


@respx.mock
async def test_label_creation_raises_when_no_id_returned(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"issueLabels": {"nodes": []}}}),
            httpx.Response(
                200, json={"data": {"issueLabelCreate": {"success": False, "issueLabel": None}}}
            ),
        ]
    )

    with pytest.raises(LinearError):
        await service.ensure_label_id("team-git", "Security")


# --- create_issue ---------------------------------------------------------------


@respx.mock
async def test_creates_an_issue(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {"id": "issue-1", "identifier": "GIT-42", "url": "https://linear.app/x"},
                    }
                }
            },
        )
    )

    issue_id, identifier = await service.create_issue("team-git", "[GitHub Issue] Bug", "body")

    assert (issue_id, identifier) == ("issue-1", "GIT-42")
    body = route.calls.last.request.read().decode()
    assert "issueCreate" in body
    assert "team-git" in body


@respx.mock
async def test_create_issue_passes_label_ids(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {"id": "issue-1", "identifier": "GIT-42", "url": "https://linear.app/x"},
                    }
                }
            },
        )
    )

    await service.create_issue("team-git", "title", "body", label_ids=["label-1", "label-2"])

    body = route.calls.last.request.read().decode()
    assert '"labelIds"' in body
    assert "label-1" in body
    assert "label-2" in body


@respx.mock
async def test_create_issue_omits_label_ids_when_none_given(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "issueCreate": {
                        "success": True,
                        "issue": {"id": "issue-1", "identifier": "GIT-42", "url": "https://linear.app/x"},
                    }
                }
            },
        )
    )

    await service.create_issue("team-git", "title", "body")

    body = route.calls.last.request.read().decode()
    assert "labelIds" not in body


@respx.mock
async def test_create_issue_raises_when_no_id_returned(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"issueCreate": {"success": False, "issue": None}}})
    )

    with pytest.raises(LinearError):
        await service.create_issue("team-git", "title", "body")


@respx.mock
async def test_create_issue_graphql_error_surfaces(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Team not found"}]})
    )

    with pytest.raises(LinearError, match="Team not found"):
        await service.create_issue("team-git", "title", "body")


# --- update_issue_content -------------------------------------------------------


@respx.mock
async def test_updates_title_and_description(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})
    )

    await service.update_issue_content("issue-1", title="New title", description="New body")

    body = route.calls.last.request.read().decode()
    assert "New title" in body
    assert "New body" in body


@respx.mock
async def test_updates_title_only(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})
    )

    await service.update_issue_content("issue-1", title="New title")

    body = route.calls.last.request.read().decode()
    assert "New title" in body
    assert "description" not in body


async def test_neither_field_is_a_no_op(service: LinearService) -> None:
    """No GraphQL call at all when there's nothing to update."""
    with respx.mock:
        route = respx.post(LINEAR_GRAPHQL_URL)
        await service.update_issue_content("issue-1")
        assert route.call_count == 0
