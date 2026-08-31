"""Linear GraphQL access.

Phase 2 added the transport plus the `viewer` query OAuth needs. Phase 3 adds
agent-activity emission — the channel through which AIDA-MATE reports progress
back into a Linear agent session. Phase 6 adds issue reads, comments, and
labels on top of the same client.

Raw `httpx` rather than a GraphQL client library: the query surface is a
handful of hand-written strings, and staying on plain httpx keeps one HTTP
stack and one mocking story (`respx`) across both Linear and GitHub.
"""

from typing import Any
from uuid import uuid4

import httpx

from app.core.errors import LinearError, LinearServerError, LinearUnavailableError
from app.core.interfaces import IPostedCommentRepository
from app.core.logging import get_logger
from app.core.retry import retry_async
from app.models.linear import AgentActivityType, LinearAttachment, LinearIssue
from app.models.posted_comment import PostedComment

logger = get_logger(__name__)

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

VIEWER_QUERY = """
query Viewer {
  viewer { id name email }
  organization { id name urlKey }
}
"""

AGENT_ACTIVITY_CREATE_MUTATION = """
mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    success
  }
}
"""

ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
    team { id }
    assignee { id }
    attachments {
      nodes { id url title sourceType metadata }
    }
  }
}
"""

COMMENT_CREATE_MUTATION = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id url }
  }
}
"""

#: `$id: String!`, matching ISSUE_UPDATE_MUTATION's top-level-entity-id
#: convention in this file (`ID!` is reserved for relation-filter fields
#: like `$teamId` — see WORKFLOW_STATES_QUERY's docstring and CLAUDE.md §8's
#: `$teamId: String!` lesson). Not yet independently confirmed against the
#: real API via introspection — do that before trusting this in production.
COMMENT_DELETE_MUTATION = """
mutation CommentDelete($id: String!) {
  commentDelete(id: $id) {
    success
  }
}
"""

#: Used by the GitHub-merge-syncs-Linear-to-Done action (CLAUDE.md §1b) to
#: resolve which workflow state counts as "Done" for a team.
#: `$teamId: ID!`, not `String!` — Linear's schema rejects the latter with
#: "used in position expecting type ID" (GRAPHQL_VALIDATION_FAILED),
#: confirmed live against the real API.
WORKFLOW_STATES_QUERY = """
query WorkflowStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name type }
  }
}
"""

#: The first write to an issue's *status* in this codebase — every other
#: Linear write (`COMMENT_CREATE_MUTATION`, labels) leaves status untouched.
ISSUE_UPDATE_MUTATION = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
  }
}
"""

#: Linear workflow-state `type` values that mean "this issue is finished."
#: Same taxonomy the inbound webhook side already keys off of in
#: `app/api/linear_webhook.py::_extract_issue_done_trigger`.
_COMPLETED_STATE_TYPE = "completed"

#: Used by the GitHub-Issues/vulnerabilities-sync action (CLAUDE.md §1c) to
#: resolve the target team for newly created issues, by its human-readable key
#: (e.g. "GIT") rather than its opaque id.
TEAMS_QUERY = "query { teams { nodes { id key name } } }"

#: The first *creation* of a Linear issue in this codebase — every other
#: write (comments, state) previously acted on an issue that already existed.
ISSUE_CREATE_MUTATION = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url }
  }
}
"""

#: Team-scoped label lookup, for tagging synced GitHub Issues/vulnerabilities
#: (CLAUDE.md §1c) with a visible Linear label rather than only a title
#: prefix. `$teamId: ID!`, matching WORKFLOW_STATES_QUERY's confirmed shape
#: — not re-verified independently for this query, but the same `team: {
#: id: { eq: $teamId } }` filter idiom.
TEAM_LABELS_QUERY = """
query TeamLabels($teamId: ID!) {
  issueLabels(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name }
  }
}
"""

ISSUE_LABEL_CREATE_MUTATION = """
mutation IssueLabelCreate($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) {
    success
    issueLabel { id name }
  }
}
"""


class LinearGraphQLClient:
    """Thin async GraphQL transport for Linear.

    Accepts an injected `httpx.AsyncClient` so tests can supply a mock
    transport and production can share a connection pool.
    """

    def __init__(self, http_client: httpx.AsyncClient, *, url: str = LINEAR_GRAPHQL_URL) -> None:
        self._http = http_client
        self._url = url

    async def execute(
        self, query: str, *, access_token: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a GraphQL document and return its `data` payload.

        A network-level failure or a 5xx response is retried with bounded
        exponential backoff (`app/core/retry.py`) — a 4xx, a non-JSON body,
        or a GraphQL `errors` array are not, since retrying those changes
        nothing.

        Raises:
            LinearError: on non-2xx-4xx status, a non-JSON body, or a
                GraphQL `errors` array. GraphQL returns HTTP 200 for
                application-level errors, so checking status alone would
                silently accept failures.
            LinearUnavailableError: the request never reached Linear at
                all, even after retrying.
            LinearServerError: Linear returned a 5xx, even after retrying.
        """
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        async def _attempt() -> dict[str, Any]:
            try:
                response = await self._http.post(
                    self._url,
                    json=payload,
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise LinearUnavailableError(f"Linear GraphQL request failed: {type(exc).__name__}") from exc

            if response.status_code >= 500:
                raise LinearServerError(f"Linear GraphQL returned HTTP {response.status_code}")
            if response.status_code >= 400:
                # Body may echo the request; log only the status to avoid leaking the token.
                raise LinearError(f"Linear GraphQL returned HTTP {response.status_code}")

            try:
                body = response.json()
            except ValueError as exc:
                raise LinearError("Linear GraphQL returned a non-JSON response") from exc

            if errors := body.get("errors"):
                messages = "; ".join(str(error.get("message", error)) for error in errors)
                raise LinearError(f"Linear GraphQL error: {messages}")

            data = body.get("data")
            if data is None:
                raise LinearError("Linear GraphQL response contained no data")
            return data

        return await retry_async(
            _attempt,
            is_retryable=lambda exc: isinstance(exc, (LinearUnavailableError, LinearServerError)),
        )


class LinearViewer:
    """Identity of the authenticated actor and its workspace.

    A plain container rather than a Pydantic model: it is an internal
    intermediate consumed immediately by the auth service.
    """

    def __init__(self, actor_id: str, organization_id: str, organization_name: str | None) -> None:
        self.actor_id = actor_id
        self.organization_id = organization_id
        self.organization_name = organization_name


async def fetch_viewer(client: LinearGraphQLClient, access_token: str) -> LinearViewer:
    """Discover who the token belongs to and which workspace it covers.

    With `actor=app`, `viewer.id` is AIDA-MATE's own app-user ID — the value that
    delegation and assignment events must be matched against. Discovering it
    here means operators never have to look it up and paste it into config.
    """
    data = await client.execute(VIEWER_QUERY, access_token=access_token)

    viewer = data.get("viewer") or {}
    organization = data.get("organization") or {}

    actor_id = viewer.get("id")
    organization_id = organization.get("id")
    if not actor_id or not organization_id:
        raise LinearError("Linear viewer query did not return an actor and organization ID")

    return LinearViewer(
        actor_id=str(actor_id),
        organization_id=str(organization_id),
        organization_name=organization.get("name"),
    )


class LinearService:
    """Operations AIDA-MATE performs against Linear.

    Token acquisition is delegated to the auth service, so nothing here has to
    know about OAuth, expiry, or refresh.
    """

    def __init__(
        self,
        graphql: LinearGraphQLClient,
        auth_service: Any,
        *,
        posted_comment_repository: IPostedCommentRepository | None = None,
        base_url: str | None = None,
    ) -> None:
        self._graphql = graphql
        self._auth = auth_service
        # Both optional and only used together (see `add_comment`) — a
        # delete link needs somewhere to point (`base_url`) and somewhere to
        # record the token it points at (`posted_comment_repository`).
        # Neither is required by any other `LinearService` method, so
        # existing construction sites/tests that don't pass them keep
        # working unchanged, just without a delete link on posted comments.
        self._posted_comment_repository = posted_comment_repository
        self._base_url = base_url

    async def get_issue(self, issue_id: str, *, organization_id: str | None = None) -> LinearIssue:
        """Fetch an issue with the attachments the PR resolver needs.

        Raises:
            LinearError: if the issue does not exist or the query fails.
        """
        access_token = await self._auth.get_access_token(organization_id)
        data = await self._graphql.execute(
            ISSUE_QUERY, access_token=access_token, variables={"id": issue_id}
        )

        payload = data.get("issue")
        if not payload:
            raise LinearError(f"Linear issue {issue_id} was not found")

        attachment_nodes = ((payload.get("attachments") or {}).get("nodes")) or []
        attachments = [
            LinearAttachment(
                id=node.get("id"),
                url=node.get("url") or "",
                title=node.get("title"),
                source_type=node.get("sourceType"),
                metadata=node.get("metadata") or {},
            )
            for node in attachment_nodes
            if node.get("url")
        ]

        return LinearIssue(
            id=payload.get("id") or issue_id,
            identifier=payload.get("identifier") or "",
            title=payload.get("title") or "",
            description=payload.get("description"),
            url=payload.get("url"),
            team_id=(payload.get("team") or {}).get("id"),
            assignee_id=(payload.get("assignee") or {}).get("id"),
            attachments=attachments,
        )

    async def add_comment(self, issue_id: str, body: str, *, organization_id: str | None = None) -> str:
        """Post a comment on an issue. Returns the new comment's id.

        Used to report outcomes on the assignment path, which has no agent
        session to emit activities into.

        When `posted_comment_repository`/`base_url` are configured (always
        true in production, see `main.py`), a "delete this comment" link is
        appended to `body` first, pointing at
        `app/api/comment_deletion.py`'s confirmation page. The link's token
        is unrelated to the comment's own id — resolving one to the other is
        exactly what `posted_comment_repository` is for.
        """
        full_body = body
        token: str | None = None
        if self._posted_comment_repository is not None and self._base_url is not None:
            token = str(uuid4())
            delete_url = f"{self._base_url}/comments/{token}/delete"
            full_body = f"{body}\n\n---\n[Delete this comment]({delete_url})"

        access_token = await self._auth.get_access_token(organization_id)
        data = await self._graphql.execute(
            COMMENT_CREATE_MUTATION,
            access_token=access_token,
            variables={"input": {"issueId": issue_id, "body": full_body}},
        )
        comment_id = ((data.get("commentCreate") or {}).get("comment") or {}).get("id")

        if token is not None and comment_id:
            try:
                await self._posted_comment_repository.save(
                    PostedComment(id=token, linear_comment_id=comment_id, organization_id=organization_id)
                )
            except Exception:
                # The comment itself already posted successfully above — a
                # bookkeeping failure here must not look like the comment
                # post failed. Worst case, this one comment's delete link
                # 404s; nothing else is affected.
                logger.exception(
                    "Could not record posted-comment delete-link mapping",
                    extra={"linear_issue_id": issue_id},
                )

        logger.info("Posted Linear comment", extra={"linear_issue_id": issue_id})
        return comment_id or ""

    async def delete_comment(self, comment_id: str, *, organization_id: str | None = None) -> None:
        """Delete a comment AIDA-MATE previously posted."""
        access_token = await self._auth.get_access_token(organization_id)
        await self._graphql.execute(
            COMMENT_DELETE_MUTATION, access_token=access_token, variables={"id": comment_id}
        )
        logger.info("Deleted Linear comment", extra={"linear_comment_id": comment_id})

    async def emit_agent_activity(
        self,
        session_id: str,
        activity_type: AgentActivityType,
        body: str,
        *,
        organization_id: str | None = None,
    ) -> None:
        """Post an activity into a Linear agent session.

        This is how AIDA-MATE reports progress. Linear derives the session's
        state from the most recent activity, and expects the first one within
        ten seconds of the `created` event — after which the session is shown
        as unresponsive.

        Raises:
            LinearError: if the token cannot be obtained or the mutation fails.
        """
        access_token = await self._auth.get_access_token(organization_id)

        await self._graphql.execute(
            AGENT_ACTIVITY_CREATE_MUTATION,
            access_token=access_token,
            variables={
                "input": {
                    "agentSessionId": session_id,
                    "content": {"type": activity_type.value, "body": body},
                }
            },
        )

        logger.info(
            "Emitted Linear agent activity",
            extra={"agent_session_id": session_id, "activity_type": activity_type.value},
        )

    async def find_done_state_id(self, team_id: str, *, organization_id: str | None = None) -> str | None:
        """Resolve a team's completed-type workflow state id, if one exists.

        A team can have more than one `type: "completed"` state in a
        customized workflow (e.g. "Done" and "Shipped") — a state literally
        named "Done" is preferred; otherwise the first completed-type state
        returned is used. Unverified against a real multi-completed-state
        workspace, the same kind of payload-shape assumption already flagged
        on `_extract_issue_done_trigger` in `app/api/linear_webhook.py`.

        Returns None if the team has no completed-type state at all, rather
        than raising — an unusual workspace configuration, not a failure.
        """
        access_token = await self._auth.get_access_token(organization_id)
        data = await self._graphql.execute(
            WORKFLOW_STATES_QUERY, access_token=access_token, variables={"teamId": team_id}
        )

        states = ((data.get("workflowStates") or {}).get("nodes")) or []
        completed = [s for s in states if s.get("type") == _COMPLETED_STATE_TYPE]
        if not completed:
            return None

        named_done = next((s for s in completed if (s.get("name") or "").strip().lower() == "done"), None)
        return (named_done or completed[0]).get("id")

    async def update_issue_state(
        self, issue_id: str, state_id: str, *, organization_id: str | None = None
    ) -> None:
        """Move an issue to `state_id`.

        Idempotent on Linear's side — setting an issue to the state it is
        already in is a harmless no-op, so a redelivered webhook triggering
        this twice causes no visible effect.
        """
        access_token = await self._auth.get_access_token(organization_id)
        await self._graphql.execute(
            ISSUE_UPDATE_MUTATION,
            access_token=access_token,
            variables={"id": issue_id, "input": {"stateId": state_id}},
        )
        logger.info("Updated Linear issue state", extra={"linear_issue_id": issue_id, "state_id": state_id})

    async def find_team_id_by_key(self, team_key: str, *, organization_id: str | None = None) -> str | None:
        """Resolve a team's id from its human-readable key (e.g. "GIT").

        No caching — team lookups happen only when syncing a GitHub Issue or
        security alert, infrequent enough that a fresh query each time is
        simpler than invalidation logic, matching `find_done_state_id`'s own
        reasoning.
        """
        access_token = await self._auth.get_access_token(organization_id)
        data = await self._graphql.execute(TEAMS_QUERY, access_token=access_token)

        teams = ((data.get("teams") or {}).get("nodes")) or []
        match = next((t for t in teams if (t.get("key") or "").upper() == team_key.upper()), None)
        return match.get("id") if match else None

    async def list_teams(self, *, organization_id: str | None = None) -> list[dict[str, str]]:
        """List every team in the workspace, as [{"id":..., "key":..., "name":...}, ...].

        Used by ScheduledPromptDashboardService to fan the dashboard out to
        every team (CLAUDE.md §1d) — the same TEAMS_QUERY find_team_id_by_key
        already uses, just returning every match instead of filtering to one.
        """
        access_token = await self._auth.get_access_token(organization_id)
        data = await self._graphql.execute(TEAMS_QUERY, access_token=access_token)
        return ((data.get("teams") or {}).get("nodes")) or []

    async def ensure_label_id(self, team_id: str, name: str, *, organization_id: str | None = None) -> str:
        """Resolve a team's label id by name, creating it if it doesn't exist yet.

        No caching, same reasoning as `find_done_state_id`/`find_team_id_by_key`
        above — label resolution only happens when syncing a GitHub Issue or
        security alert, infrequent enough that a fresh lookup each time is
        simpler than invalidation logic.

        Raises:
            LinearError: the create mutation failed, or returned no label id.
        """
        access_token = await self._auth.get_access_token(organization_id)
        data = await self._graphql.execute(
            TEAM_LABELS_QUERY, access_token=access_token, variables={"teamId": team_id}
        )

        labels = ((data.get("issueLabels") or {}).get("nodes")) or []
        target = name.lower()
        match = next((label for label in labels if (label.get("name") or "").strip().lower() == target), None)
        if match is not None:
            return match["id"]

        data = await self._graphql.execute(
            ISSUE_LABEL_CREATE_MUTATION,
            access_token=access_token,
            variables={"input": {"name": name, "teamId": team_id}},
        )
        issue_label = (data.get("issueLabelCreate") or {}).get("issueLabel") or {}
        label_id = issue_label.get("id")
        if not label_id:
            raise LinearError(f"Linear label creation returned no id for '{name}'")

        logger.info("Created Linear label", extra={"team_id": team_id, "label_name": name})
        return label_id

    async def create_issue(
        self,
        team_id: str,
        title: str,
        description: str,
        *,
        label_ids: list[str] | None = None,
        organization_id: str | None = None,
    ) -> tuple[str, str]:
        """Create a new Linear issue on `team_id`.

        Returns (issue_id, identifier) — e.g. ("abc-123", "GIT-42"). The
        identifier is what a human recognizes; the id is what every other
        write method here takes.

        Raises:
            LinearError: the mutation failed, or returned no issue id.
        """
        input_fields: dict[str, object] = {"teamId": team_id, "title": title, "description": description}
        if label_ids:
            input_fields["labelIds"] = label_ids

        access_token = await self._auth.get_access_token(organization_id)
        data = await self._graphql.execute(
            ISSUE_CREATE_MUTATION, access_token=access_token, variables={"input": input_fields}
        )

        issue = (data.get("issueCreate") or {}).get("issue") or {}
        issue_id = issue.get("id")
        if not issue_id:
            raise LinearError("Linear issue creation returned no issue id")

        identifier = issue.get("identifier", "")
        logger.info("Created Linear issue", extra={"linear_issue_id": issue_id, "identifier": identifier})
        return issue_id, identifier

    async def update_issue_content(
        self,
        issue_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        organization_id: str | None = None,
    ) -> None:
        """Update an existing issue's title and/or description.

        Reuses `ISSUE_UPDATE_MUTATION` — `issueUpdate(id, input)` is generic;
        `update_issue_state` above just happens to only ever pass `stateId`
        in `input`. No new mutation needed for a content-only update.
        """
        input_fields: dict[str, str] = {}
        if title is not None:
            input_fields["title"] = title
        if description is not None:
            input_fields["description"] = description
        if not input_fields:
            return

        access_token = await self._auth.get_access_token(organization_id)
        await self._graphql.execute(
            ISSUE_UPDATE_MUTATION,
            access_token=access_token,
            variables={"id": issue_id, "input": input_fields},
        )
        logger.info("Updated Linear issue content", extra={"linear_issue_id": issue_id})
