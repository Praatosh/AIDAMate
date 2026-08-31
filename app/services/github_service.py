"""GitHub REST access.

All GitHub HTTP lives here. Agents and the resolver call typed methods; no
module outside this one constructs a GitHub URL or handles a GitHub status
code. That is what makes a later swap to a GitHub MCP server a change of
implementation rather than a change to callers.

Two credential paths, both behind `IGitHubCredentials`:

* `GitHubAppCredentials` — the production path. Signs a short-lived RS256 JWT
  with the App private key, trades it for an installation token, and caches
  that token until shortly before it expires.
* `StaticTokenCredentials` — a development PAT, for working before an App is
  registered.

Raw `httpx` rather than PyGithub: PyGithub is synchronous, which would mean
thread-pool wrapping every call inside an async service, and it is harder to
intercept cleanly in tests than plain httpx with `respx`.
"""

import base64
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.errors import (
    GitHubError,
    GitHubRateLimitError,
    GitHubServerError,
    GitHubUnavailableError,
    InvalidPullRequestError,
    PullRequestNotMergeableError,
)
from app.core.logging import get_logger
from app.core.retry import retry_async
from app.models.github import (
    ChangedFile,
    Commit,
    FileChangeStatus,
    PullRequest,
    PullRequestRef,
    RepositoryRef,
)

logger = get_logger(__name__)

GITHUB_API_VERSION = "2022-11-28"

#: Ceiling on `download_archive`'s response body. Unlike every other bounded
#: read in this codebase (`read_file`'s `max_bytes`, `_get_paginated`'s
#: `max_pages`), this one had no limit at all until a security audit flagged
#: it — a PR whose head commit's tree contains a very large blob would be
#: buffered into memory in full, with no ceiling. 500 MB comfortably covers
#: any real repository this app reviews while still bounding worst-case
#: memory use per review.
_MAX_ARCHIVE_BYTES = 500 * 1024 * 1024

#: GitHub caps App JWTs at ten minutes; nine leaves room for clock skew.
_JWT_LIFETIME_SECONDS = 9 * 60

#: Refresh an installation token this far before it expires.
_TOKEN_REFRESH_LEEWAY = timedelta(minutes=5)


class IGitHubCredentials(Protocol):
    """Supplies a bearer token for GitHub REST calls."""

    async def get_token(self) -> str: ...


class StaticTokenCredentials:
    """A fixed personal access token. Development only."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_token(self) -> str:
        """Return the configured token."""
        return self._token


class GitHubAppCredentials:
    """Mints and caches installation access tokens for a GitHub App."""

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key: str,
        http_client: httpx.AsyncClient,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = _normalize_private_key(private_key)
        self._http = http_client
        self._api_url = api_url.rstrip("/")
        self._token: str | None = None
        self._expires_at: datetime | None = None

    async def get_token(self) -> str:
        """Return a valid installation token, minting a new one if needed."""
        if self._token and self._expires_at and datetime.now(UTC) < self._expires_at - _TOKEN_REFRESH_LEEWAY:
            return self._token

        jwt_token = self._build_app_jwt()
        url = f"{self._api_url}/app/installations/{self._installation_id}/access_tokens"

        try:
            response = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                },
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub installation token request failed: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise GitHubError(f"GitHub installation token request returned HTTP {response.status_code}")

        body = response.json()
        token = body.get("token")
        if not token:
            raise GitHubError("GitHub installation token response contained no token")

        self._token = token
        self._expires_at = _parse_github_timestamp(body.get("expires_at"))
        logger.info("Minted GitHub installation token", extra={"installation_id": self._installation_id})
        return token

    def _build_app_jwt(self) -> str:
        """Sign a short-lived RS256 JWT identifying the App.

        Imported lazily so the App path's crypto dependency is only required by
        deployments that actually use it.
        """
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise GitHubError(
                "GitHub App authentication requires the 'pyjwt[crypto]' package"
            ) from exc

        now = int(time.time())
        return jwt.encode(
            # `iat` is backdated 60s because GitHub rejects tokens whose issue
            # time is even slightly in the future relative to its own clock.
            {"iat": now - 60, "exp": now + _JWT_LIFETIME_SECONDS, "iss": self._app_id},
            self._private_key,
            algorithm="RS256",
        )


def _normalize_private_key(raw: str) -> str:
    """Accept a PEM with escaped newlines, or a base64-encoded PEM.

    Multi-line values are awkward in `.env` files and CI secret stores, so both
    common workarounds are supported.
    """
    if "-----BEGIN" in raw:
        return raw.replace("\\n", "\n")

    try:
        decoded = base64.b64decode(raw, validate=True).decode()
    except Exception as exc:
        raise GitHubError("GITHUB_PRIVATE_KEY is neither a PEM nor valid base64") from exc

    if "-----BEGIN" not in decoded:
        raise GitHubError("GITHUB_PRIVATE_KEY decoded to something that is not a PEM key")
    return decoded


def _parse_github_timestamp(value: object) -> datetime | None:
    """Parse GitHub's ISO-8601 `...Z` timestamps."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _quoted_search_phrase(text: str) -> str:
    """Sanitize `text` for embedding inside a `"..."` GitHub search qualifier.

    Security-audit finding: `search_pull_requests`/`search_pull_requests_referencing`
    previously embedded `text` directly (`f'... in:title,body "{text}"'`) with
    no escaping. GitHub's search syntax has no way to escape a literal `"`
    inside a quoted phrase, so a `text` value containing one would break out
    of the phrase and let the rest of it be interpreted as additional search
    qualifiers (e.g. appending `author:` or `repo:` terms). Today's two
    callers only ever pass a Linear issue identifier or `f"#{number}"` —
    neither meaningfully attacker-controlled — but stripping embedded quotes
    here closes the gap for any future caller that passes freeform text (a
    PR/issue title, say) without needing to re-audit every call site.
    """
    return text.replace('"', "")


class GitHubService:
    """Typed access to the GitHub REST endpoints AIDA-MATE uses."""

    def __init__(
        self,
        credentials: IGitHubCredentials,
        http_client: httpx.AsyncClient,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._credentials = credentials
        self._http = http_client
        self._api_url = api_url.rstrip("/")

    # -- Reads --------------------------------------------------------------

    async def get_pull_request(self, ref: PullRequestRef) -> PullRequest:
        """Fetch a pull request's metadata, changed files, and commits."""
        repo = ref.repository
        payload = await self._get(f"/repos/{repo.owner}/{repo.name}/pulls/{ref.number}")

        head = payload.get("head") or {}
        base = payload.get("base") or {}
        head_repo = head.get("repo") or {}

        return PullRequest(
            ref=ref,
            title=payload.get("title") or "",
            body=payload.get("body"),
            author=(payload.get("user") or {}).get("login"),
            state=payload.get("state") or "open",
            draft=bool(payload.get("draft")),
            base_ref=base.get("ref") or "",
            head_ref=head.get("ref") or "",
            head_sha=head.get("sha") or "",
            is_fork=(head_repo.get("full_name") or "").lower() != repo.full_name.lower(),
            changed_files=await self.get_pull_request_files(ref),
            commits=await self.get_pull_request_commits(ref),
        )

    async def get_pull_request_files(self, ref: PullRequestRef) -> list[ChangedFile]:
        """List the files a pull request changes, with their diff hunks."""
        repo = ref.repository
        pages = await self._get_paginated(f"/repos/{repo.owner}/{repo.name}/pulls/{ref.number}/files")

        files: list[ChangedFile] = []
        for item in pages:
            try:
                status = FileChangeStatus(item.get("status", "modified"))
            except ValueError:
                # An unrecognised status must not discard the file: it still
                # changed, and its path still drives area detection.
                status = FileChangeStatus.CHANGED

            files.append(
                ChangedFile(
                    filename=item.get("filename", ""),
                    status=status,
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                    previous_filename=item.get("previous_filename"),
                    patch=item.get("patch"),
                )
            )
        return files

    async def get_pull_request_commits(self, ref: PullRequestRef) -> list[Commit]:
        """List a pull request's commits."""
        repo = ref.repository
        pages = await self._get_paginated(f"/repos/{repo.owner}/{repo.name}/pulls/{ref.number}/commits")

        return [
            Commit(
                sha=item.get("sha", ""),
                message=(item.get("commit") or {}).get("message", ""),
                author=((item.get("commit") or {}).get("author") or {}).get("name"),
            )
            for item in pages
        ]

    async def get_pull_request_diff(self, ref: PullRequestRef) -> str:
        """Fetch the raw unified diff for a pull request."""
        repo = ref.repository
        return await self._get_raw(
            f"/repos/{repo.owner}/{repo.name}/pulls/{ref.number}",
            accept="application/vnd.github.v3.diff",
        )

    async def download_archive(self, repo: RepositoryRef, sha: str) -> bytes:
        """Fetch repository content at `sha` via GitHub's tarball endpoint.

        Bypasses `_request`/`_get`: the body is binary (never JSON-decoded),
        and the endpoint 302s to a signed, time-limited download URL that
        httpx will NOT follow unless told to — httpx defaults to
        `follow_redirects=False` on both `Client` and `AsyncClient` (unlike
        `requests`, which follows by default), confirmed by reading
        `httpx/_client.py` directly rather than assuming. Passed here as a
        per-call override; the shared `app.state.http_client` keeps its safe
        default for every other call site.

        The redirect target is a different host (`codeload.github.com` or a
        signed storage URL), and httpx strips the `Authorization` header on a
        cross-origin redirect by default — exactly the desired behavior, so
        the GitHub token is never forwarded to that third-party host.

        Transient failures (a dropped connection, a momentary 5xx) are
        retried with bounded exponential backoff (`app/core/retry.py`) — the
        404/oversize/other-4xx cases below are not, since retrying those
        changes nothing.

        Raises:
            InvalidPullRequestError: the commit no longer exists (404) — most
                commonly a force-push race between resolving the PR and
                downloading its archive.
            GitHubUnavailableError: the request never reached GitHub at all,
                even after retrying.
            GitHubServerError: GitHub returned a 5xx, even after retrying.
            GitHubError: GitHub reached, but returned another error status.
        """
        path = f"/repos/{repo.owner}/{repo.name}/tarball/{sha}"

        async def _attempt() -> bytes:
            try:
                async with self._http.stream(
                    "GET", f"{self._api_url}{path}", headers=await self._headers(), follow_redirects=True
                ) as response:
                    if response.status_code == 404:
                        raise InvalidPullRequestError(
                            f"No archive available for {repo.full_name}@{sha}; the commit may no longer exist"
                        )
                    if response.status_code >= 500:
                        raise GitHubServerError(
                            f"GitHub returned HTTP {response.status_code} for archive download"
                        )
                    if response.status_code >= 400:
                        raise GitHubError(f"GitHub returned HTTP {response.status_code} for archive download")

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_ARCHIVE_BYTES:
                            raise GitHubError(
                                f"Archive for {repo.full_name}@{sha} exceeds the "
                                f"{_MAX_ARCHIVE_BYTES // (1024 * 1024)} MB limit"
                            )
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise GitHubUnavailableError(
                    f"GitHub archive download unreachable: {type(exc).__name__}"
                ) from exc
            return b"".join(chunks)

        return await retry_async(
            _attempt,
            is_retryable=lambda exc: isinstance(
                exc, (GitHubRateLimitError, GitHubUnavailableError, GitHubServerError)
            ),
        )

    async def list_open_pull_requests(self, repo: RepositoryRef, *, limit: int = 100) -> list[dict[str, Any]]:
        """List open pull requests for a repository.

        Capped at one page. Matching a Linear ticket against hundreds of open
        PRs is a sign the allowlist is too broad, not a case worth paginating.
        """
        return await self._get(
            f"/repos/{repo.owner}/{repo.name}/pulls",
            params={"state": "open", "per_page": min(limit, 100)},
        )

    async def search_pull_requests(self, repo: RepositoryRef, text: str) -> list[dict[str, Any]]:
        """Search open pull requests whose title or body contains `text`."""
        query = f'repo:{repo.full_name} is:pr is:open in:title,body "{_quoted_search_phrase(text)}"'
        payload = await self._get("/search/issues", params={"q": query, "per_page": 20})
        return payload.get("items", []) if isinstance(payload, dict) else []

    async def list_pull_requests_for_commit(self, repo: RepositoryRef, sha: str) -> list[dict[str, Any]]:
        """List pull requests associated with a commit.

        The reliable relationship signal for security alerts (CLAUDE.md §1c):
        they carry a commit SHA in their own payload but never a direct PR
        reference, unlike `pull_request` events which name their PR outright.
        """
        return await self._get(f"/repos/{repo.owner}/{repo.name}/commits/{sha}/pulls")

    async def search_pull_requests_referencing(self, repo: RepositoryRef, text: str) -> list[dict[str, Any]]:
        """Search pull requests — open OR closed — whose title or body contains `text`.

        Used to find the PR that references a GitHub Issue (CLAUDE.md §1c),
        where the linking PR may already be merged. Deliberately separate
        from `search_pull_requests` above (which stays `is:open`-only for
        `pr_resolver.py`'s use case) rather than adding a parameter to it, so
        that already-tested PR-resolution path is untouched.
        """
        query = f'repo:{repo.full_name} is:pr in:title,body "{_quoted_search_phrase(text)}"'
        payload = await self._get("/search/issues", params={"q": query, "per_page": 20})
        return payload.get("items", []) if isinstance(payload, dict) else []

    async def get_default_branch(self, repo: RepositoryRef) -> str:
        """Resolve a repository's default branch name.

        Used by scheduled prompts (CLAUDE.md §1d) when a schedule doesn't
        pin a specific branch — every other GitHub call in this codebase
        already has a PR's `head_ref` in hand and never needed this.
        """
        payload = await self._get(f"/repos/{repo.owner}/{repo.name}")
        return payload.get("default_branch") or "main"

    async def get_pull_request_head_sha(self, ref: PullRequestRef) -> str:
        """Resolve a pull request's current head commit sha.

        Used by scheduled prompts (CLAUDE.md §1d) when a schedule targets a
        specific PR rather than a branch. Deliberately lighter than
        `get_pull_request`: that method also fetches changed files and
        commits (two extra API calls) for the review pipeline's diff-based
        analysis; a scheduled prompt only needs the head sha to pin its
        archive download, the same way `get_commit_sha` does for a branch.
        """
        repo = ref.repository
        payload = await self._get(f"/repos/{repo.owner}/{repo.name}/pulls/{ref.number}")
        sha = (payload.get("head") or {}).get("sha")
        if not sha:
            raise GitHubError(f"GitHub returned no head sha for {ref.slug}")
        return sha

    async def get_commit_sha(self, repo: RepositoryRef, ref: str) -> str:
        """Resolve `ref` (a branch, tag, or sha) to its current commit sha.

        Used by scheduled prompts (CLAUDE.md §1d) to pin the exact snapshot
        a run's archive download targets, the same way a PR's `head_sha`
        already pins the review pipeline's.
        """
        repo_owner_name = f"{repo.owner}/{repo.name}"
        payload = await self._get(f"/repos/{repo_owner_name}/commits/{quote(ref, safe='')}")
        sha = payload.get("sha")
        if not sha:
            raise GitHubError(f"GitHub returned no commit sha for {repo_owner_name}@{ref}")
        return sha

    # -- Writes -------------------------------------------------------------

    async def ensure_labels_exist(self, repo: RepositoryRef, labels: dict[str, str]) -> None:
        """Create any of `labels` (name -> hex colour) that the repo lacks.

        Idempotent. A label that already exists is left alone — including its
        colour, since a maintainer may have deliberately recoloured it.

        Failures here are logged and swallowed: a repository may forbid label
        creation, and losing one label is a far better outcome than losing the
        whole review.
        """
        existing = {item.get("name") for item in await self._get_paginated(
            f"/repos/{repo.owner}/{repo.name}/labels"
        )}

        for name, color in labels.items():
            if name in existing:
                continue
            try:
                await self._post(
                    f"/repos/{repo.owner}/{repo.name}/labels",
                    json={"name": name, "color": color, "description": "Applied by AIDA-MATE"},
                )
                logger.info("Created GitHub label", extra={"repository": repo.full_name, "label": name})
            except GitHubError as exc:
                logger.warning(
                    "Could not create GitHub label",
                    extra={"repository": repo.full_name, "label": name, "error": str(exc)},
                )

    async def apply_labels(
        self,
        ref: PullRequestRef,
        labels: set[str],
        *,
        exclusive_prefixes: tuple[str, ...] = (),
    ) -> None:
        """Apply `labels`, first removing stale members of exclusive namespaces.

        Without the removal step a re-reviewed pull request would accumulate
        `risk: low` alongside `risk: high` and the signal would be meaningless.
        Only AIDA-MATE's own namespaces are touched — labels a human added are
        never removed.
        """
        repo = ref.repository
        current = {
            item.get("name")
            for item in await self._get_paginated(
                f"/repos/{repo.owner}/{repo.name}/issues/{ref.number}/labels"
            )
        }

        stale = {
            name
            for name in current
            if name
            and any(name.startswith(f"{prefix}:") for prefix in exclusive_prefixes)
            and name not in labels
        }
        for name in stale:
            try:
                await self._delete(
                    f"/repos/{repo.owner}/{repo.name}/issues/{ref.number}/labels/{quote(name, safe='')}"
                )
            except GitHubError as exc:
                logger.warning("Could not remove stale label", extra={"label": name, "error": str(exc)})

        missing = sorted(labels - current)
        if missing:
            await self._post(
                f"/repos/{repo.owner}/{repo.name}/issues/{ref.number}/labels",
                json={"labels": missing},
            )

        logger.info(
            "Applied GitHub labels",
            extra={"github_pr": ref.slug, "added": missing, "removed": sorted(stale)},
        )

    async def add_comment(self, ref: PullRequestRef, body: str, *, marker: str | None = None) -> int:
        """Post a comment, or update AIDA-MATE's existing one when `marker` matches.

        Updating in place keeps one comment per pull request across re-reviews,
        instead of a growing pile that buries the current verdict.

        Uses the Issues comment API: on GitHub a pull request is an issue, and
        this posts to the conversation timeline rather than to a diff line.
        """
        repo = ref.repository

        if marker:
            existing_id = await self._find_comment_id(ref, marker)
            if existing_id is not None:
                await self._patch(
                    f"/repos/{repo.owner}/{repo.name}/issues/comments/{existing_id}",
                    json={"body": body},
                )
                logger.info(
                    "Updated AIDA-MATE comment",
                    extra={"github_pr": ref.slug, "comment_id": existing_id},
                )
                return existing_id

        created = await self._post(
            f"/repos/{repo.owner}/{repo.name}/issues/{ref.number}/comments", json={"body": body}
        )
        comment_id = int(created.get("id", 0))
        logger.info("Posted AIDA-MATE comment", extra={"github_pr": ref.slug, "comment_id": comment_id})
        return comment_id

    async def merge_pull_request(self, ref: PullRequestRef, *, merge_method: str = "merge") -> None:
        """Merge a pull request into its base branch. See CLAUDE.md §1a.

        Bypasses `_request`, like `download_archive` does: a 405 here means
        "not mergeable right now" (conflicts, failing required checks, branch
        protection) — an expected, common outcome that needs its own error
        type, not the generic status-code mapping every other call site shares.
        """
        repo = ref.repository
        path = f"/repos/{repo.owner}/{repo.name}/pulls/{ref.number}/merge"
        try:
            response = await self._http.put(
                f"{self._api_url}{path}", headers=await self._headers(), json={"merge_method": merge_method}
            )
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"GitHub merge request unreachable: {type(exc).__name__}") from exc

        if response.status_code == 405:
            raise PullRequestNotMergeableError(f"{ref.slug} is not mergeable")
        if response.status_code == 404:
            raise GitHubError(f"{ref.slug} not found")
        if response.status_code >= 400:
            raise GitHubError(f"Merging {ref.slug} failed with HTTP {response.status_code}")

        logger.info("Merged pull request", extra={"github_pr": ref.slug, "merge_method": merge_method})

    async def close_issue(self, repo: RepositoryRef, number: int) -> None:
        """Close a GitHub Issue. See CLAUDE.md §1c.

        Ordinary PATCH through `_request`'s generic status-code mapping —
        unlike `merge_pull_request`, closing an issue has no "expected common
        failure" status code that needs its own error type; 404/403/rate-limit
        already get sensible errors from `_request`. Idempotent: closing an
        already-closed issue is a harmless no-op on GitHub's side.
        """
        await self._patch(
            f"/repos/{repo.owner}/{repo.name}/issues/{number}",
            json={"state": "closed", "state_reason": "completed"},
        )
        logger.info("Closed GitHub issue", extra={"github_repo": repo.full_name, "github_issue": number})

    async def _find_comment_id(self, ref: PullRequestRef, marker: str) -> int | None:
        """Find AIDA-MATE's own comment on a pull request, by hidden marker."""
        repo = ref.repository
        comments = await self._get_paginated(
            f"/repos/{repo.owner}/{repo.name}/issues/{ref.number}/comments"
        )
        for comment in comments:
            if marker in (comment.get("body") or ""):
                return int(comment.get("id", 0)) or None
        return None

    # -- Transport ----------------------------------------------------------

    async def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        """Build request headers, including a freshly-resolved token."""
        return {
            "Authorization": f"Bearer {await self._credentials.get_token()}",
            "Accept": accept,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    async def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        accept: str = "application/vnd.github+json",
        params: dict | None = None,
        json: dict | None = None,
    ) -> httpx.Response:
        """Issue a request and translate GitHub's failure modes into domain errors.

        A network-level failure, a rate limit, or a 5xx response is retried
        with bounded exponential backoff (`app/core/retry.py`) — every other
        4xx below is not, since retrying those changes nothing.
        """

        async def _attempt() -> httpx.Response:
            try:
                response = await self._http.request(
                    method,
                    f"{self._api_url}{path}",
                    headers=await self._headers(accept),
                    params=params,
                    json=json,
                )
            except httpx.HTTPError as exc:
                raise GitHubUnavailableError(f"GitHub request failed: {type(exc).__name__}") from exc

            # 403 with an exhausted quota is a rate limit, not an auth failure —
            # distinguishing them decides whether retrying could ever help.
            if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
                raise GitHubRateLimitError(f"GitHub rate limit exhausted on {path}")

            if response.status_code == 403:
                # The other common 403 is a token lacking a permission. Say so,
                # because "add Issues: Read and write" is an actionable fix and
                # "HTTP 403" is not.
                raise GitHubError(
                    f"GitHub denied {method} {path} (HTTP 403). The token is likely missing a "
                    "required permission."
                )

            if response.status_code == 404:
                raise GitHubError(f"GitHub resource not found: {path}")

            if response.status_code >= 500:
                raise GitHubServerError(f"GitHub returned HTTP {response.status_code} for {path}")

            if response.status_code >= 400:
                raise GitHubError(f"GitHub returned HTTP {response.status_code} for {path}")

            return response

        return await retry_async(
            _attempt,
            is_retryable=lambda exc: isinstance(
                exc, (GitHubRateLimitError, GitHubUnavailableError, GitHubServerError)
            ),
        )

    async def _post(self, path: str, *, json: dict) -> Any:
        """POST JSON and decode the response."""
        response = await self._request(path, method="POST", json=json)
        try:
            return response.json()
        except ValueError:
            return {}

    async def _patch(self, path: str, *, json: dict) -> Any:
        """PATCH JSON and decode the response."""
        response = await self._request(path, method="PATCH", json=json)
        try:
            return response.json()
        except ValueError:
            return {}

    async def _delete(self, path: str) -> None:
        """DELETE a resource."""
        await self._request(path, method="DELETE")

    async def _get(self, path: str, *, params: dict | None = None) -> Any:
        """GET and decode a JSON response."""
        response = await self._request(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError(f"GitHub returned a non-JSON response for {path}") from exc

    async def _get_raw(self, path: str, *, accept: str) -> str:
        """GET a non-JSON representation, such as a diff."""
        return (await self._request(path, accept=accept)).text

    async def _get_paginated(self, path: str, *, per_page: int = 100, max_pages: int = 10) -> list[dict]:
        """Collect a paginated list endpoint.

        Bounded at `max_pages`: a pull request with more than a thousand
        changed files is beyond useful review anyway, and an unbounded loop
        here would let one enormous PR exhaust the rate limit.
        """
        collected: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = await self._get(path, params={"per_page": per_page, "page": page})
            if not isinstance(batch, list) or not batch:
                break
            collected.extend(batch)
            if len(batch) < per_page:
                break
        else:
            logger.warning(
                "Truncated paginated GitHub response",
                extra={"path": path, "max_pages": max_pages},
            )
        return collected
