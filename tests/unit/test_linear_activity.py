"""Agent-activity emission to Linear."""

import httpx
import pytest
import respx

from app.core.errors import LinearError
from app.models.linear import AgentActivityType
from app.services.linear_service import LINEAR_GRAPHQL_URL, LinearGraphQLClient, LinearService


class StubAuth:
    """Supplies a token without touching OAuth."""

    def __init__(self, token: str = "token-1", fail: bool = False) -> None:
        self._token = token
        self._fail = fail
        self.requested_orgs: list[str | None] = []

    async def get_access_token(self, organization_id=None) -> str:
        self.requested_orgs.append(organization_id)
        if self._fail:
            raise LinearError("no installation")
        return self._token


@pytest.fixture
def service_and_auth():
    auth = StubAuth()
    return LinearService(LinearGraphQLClient(httpx.AsyncClient()), auth), auth


@respx.mock
async def test_emits_thought_activity(service_and_auth) -> None:
    service, _ = service_and_auth
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}})
    )

    await service.emit_agent_activity("session-1", AgentActivityType.THOUGHT, "Looking…")

    body = route.calls.last.request.read().decode()
    assert "agentActivityCreate" in body
    assert "session-1" in body
    assert "thought" in body


@respx.mock
async def test_uses_the_workspace_token(service_and_auth) -> None:
    service, auth = service_and_auth
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}})
    )

    await service.emit_agent_activity(
        "s-1", AgentActivityType.RESPONSE, "Done", organization_id="org-9"
    )

    assert auth.requested_orgs == ["org-9"]
    assert route.calls.last.request.headers["authorization"] == "Bearer token-1"


@respx.mock
@pytest.mark.parametrize(
    "activity_type", [AgentActivityType.THOUGHT, AgentActivityType.ERROR, AgentActivityType.RESPONSE]
)
async def test_all_activity_types_serialize_to_their_string(service_and_auth, activity_type) -> None:
    service, _ = service_and_auth
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}})
    )

    await service.emit_agent_activity("s-1", activity_type, "body")

    assert f'"{activity_type.value}"' in route.calls.last.request.read().decode()


@respx.mock
async def test_graphql_errors_surface_as_linear_errors(service_and_auth) -> None:
    """GraphQL returns HTTP 200 for application errors, so status alone is not enough."""
    service, _ = service_and_auth
    respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Session not found"}]})
    )

    with pytest.raises(LinearError, match="Session not found"):
        await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")


@respx.mock
async def test_http_errors_surface_as_linear_errors(service_and_auth) -> None:
    service, _ = service_and_auth
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(LinearError, match="500"):
        await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")


@respx.mock
async def test_transport_failure_surfaces_as_linear_error(service_and_auth) -> None:
    service, _ = service_and_auth
    respx.post(LINEAR_GRAPHQL_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(LinearError):
        await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")


# --- Retry (app/core/retry.py) ----------------------------------------------


@respx.mock
async def test_a_server_error_is_retried_then_succeeds(service_and_auth) -> None:
    service, _ = service_and_auth
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}}),
        ]
    )

    await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")

    assert route.call_count == 2


@respx.mock
async def test_a_transport_failure_is_retried_then_succeeds(service_and_auth) -> None:
    service, _ = service_and_auth
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(200, json={"data": {"agentActivityCreate": {"success": True}}}),
        ]
    )

    await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")

    assert route.call_count == 2


@respx.mock
async def test_a_4xx_is_not_retried(service_and_auth) -> None:
    service, _ = service_and_auth
    route = respx.post(LINEAR_GRAPHQL_URL).mock(return_value=httpx.Response(403, text="forbidden"))

    with pytest.raises(LinearError):
        await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")

    assert route.call_count == 1


@respx.mock
async def test_a_graphql_errors_array_is_not_retried(service_and_auth) -> None:
    service, _ = service_and_auth
    route = respx.post(LINEAR_GRAPHQL_URL).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "Session not found"}]})
    )

    with pytest.raises(LinearError):
        await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")

    assert route.call_count == 1


async def test_missing_installation_surfaces_clearly() -> None:
    service = LinearService(LinearGraphQLClient(httpx.AsyncClient()), StubAuth(fail=True))

    with pytest.raises(LinearError):
        await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")


@respx.mock
async def test_token_is_not_echoed_into_errors(service_and_auth) -> None:
    service, _ = service_and_auth
    respx.post(LINEAR_GRAPHQL_URL).mock(return_value=httpx.Response(403, text="token-1 is invalid"))

    with pytest.raises(LinearError) as exc_info:
        await service.emit_agent_activity("s-1", AgentActivityType.THOUGHT, "x")

    assert "token-1" not in str(exc_info.value)
