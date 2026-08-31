"""Resolving and writing a Linear issue's workflow state (CLAUDE.md §1b)."""

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


def _states_response(nodes: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"data": {"workflowStates": {"nodes": nodes}}})


# --- find_done_state_id -------------------------------------------------------


@respx.mock
async def test_finds_the_only_completed_state(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=_states_response(
            [
                {"id": "state-backlog", "name": "Backlog", "type": "backlog"},
                {"id": "state-done", "name": "Done", "type": "completed"},
            ]
        )
    )

    assert await service.find_done_state_id("team-1") == "state-done"


@respx.mock
async def test_prefers_a_state_literally_named_done(service: LinearService) -> None:
    """A workspace can have more than one completed-type state (e.g. "Done" and "Shipped")."""
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=_states_response(
            [
                {"id": "state-shipped", "name": "Shipped", "type": "completed"},
                {"id": "state-done", "name": "Done", "type": "completed"},
            ]
        )
    )

    assert await service.find_done_state_id("team-1") == "state-done"


@respx.mock
async def test_done_name_match_is_case_insensitive(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=_states_response([{"id": "state-done", "name": "done", "type": "completed"}])
    )

    assert await service.find_done_state_id("team-1") == "state-done"


@respx.mock
async def test_falls_back_to_first_completed_state_when_none_is_named_done(
    service: LinearService,
) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=_states_response([{"id": "state-shipped", "name": "Shipped", "type": "completed"}])
    )

    assert await service.find_done_state_id("team-1") == "state-shipped"


@respx.mock
async def test_returns_none_when_no_completed_state_exists(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=_states_response([{"id": "state-backlog", "name": "Backlog", "type": "backlog"}])
    )

    assert await service.find_done_state_id("team-1") is None


@respx.mock
async def test_returns_none_when_team_has_no_states(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_states_response([]))

    assert await service.find_done_state_id("team-1") is None


@respx.mock
async def test_workflow_states_query_scopes_to_the_team(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(return_value=_states_response([]))

    await service.find_done_state_id("team-42")

    assert "team-42" in route.calls.last.request.read().decode()


# --- update_issue_state --------------------------------------------------------


@respx.mock
async def test_updates_the_issue_state(service: LinearService) -> None:
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})
    )

    await service.update_issue_state("issue-1", "state-done")

    body = route.calls.last.request.read().decode()
    assert "issueUpdate" in body
    assert "issue-1" in body
    assert "state-done" in body


@respx.mock
async def test_update_issue_state_graphql_error_surfaces(service: LinearService) -> None:
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Issue not found"}]})
    )

    with pytest.raises(LinearError, match="Issue not found"):
        await service.update_issue_state("issue-1", "state-done")
