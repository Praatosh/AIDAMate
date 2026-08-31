"""GitHub webhook signature verification and merge-event filtering (CLAUDE.md §1b).

Mirrors `tests/unit/test_linear_webhook.py`'s structure, adapted for GitHub's
`sha256=<hex>` signature format and event/action-based filtering instead of
Linear's `updatedFrom`-diffing.
"""

import hashlib
import hmac
import json

import pytest

from app.api.github_webhook import verify_signature

SECRET = "test-github-webhook-secret"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_valid_signature_accepted() -> None:
    body = b'{"action":"closed"}'

    assert verify_signature(body, _sig(body), SECRET) is True


def test_signature_from_wrong_secret_rejected() -> None:
    body = b'{"action":"closed"}'

    assert verify_signature(body, _sig(body, "attacker-secret"), SECRET) is False


@pytest.mark.parametrize("header", [None, "", "not-a-signature", "0" * 64])
def test_absent_or_bogus_signatures_rejected(header: str | None) -> None:
    assert verify_signature(b"{}", header, SECRET) is False


def test_linear_style_bare_hex_signature_rejected() -> None:
    """GitHub requires the `sha256=` prefix; a bare digest (Linear's format) is not valid here."""
    body = b"{}"
    bare = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert verify_signature(body, bare, SECRET) is False


def test_sha1_prefix_rejected() -> None:
    """GitHub also offers a legacy `X-Hub-Signature` (sha1); only sha256 is accepted here."""
    body = b"{}"
    digest = hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()

    assert verify_signature(body, f"sha1={digest}", SECRET) is False


def test_signature_is_body_specific() -> None:
    assert verify_signature(b'{"amount":9}', _sig(b'{"amount":1}'), SECRET) is False


def test_empty_secret_fails_closed() -> None:
    assert verify_signature(b"{}", _sig(b"{}", ""), "") is False


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class RecordingSyncService:
    def __init__(self) -> None:
        self.calls = []

    async def handle_pull_request_merged(self, ref) -> None:
        self.calls.append(ref)


def _pr_payload(**overrides) -> dict:
    payload = {
        "action": "closed",
        "repository": {"name": "api", "owner": {"login": "acme"}},
        "pull_request": {
            "number": 431,
            "html_url": "https://github.com/acme/api/pull/431",
            "merged": True,
            "base": {"ref": "main"},
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def gh_client(monkeypatch: pytest.MonkeyPatch):
    """A fresh app instance with the GitHub merge sync feature fully enabled."""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_MERGE_SYNC_ENABLED", "true")
    monkeypatch.setenv("GITHUB_REPO_ALLOWLIST", "acme/api")
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:
            recording = RecordingSyncService()
            test_client.app.state.github_merge_sync_service = recording
            test_client.app.state.recording_sync_service = recording  # convenience handle
            yield test_client
    finally:
        get_settings.cache_clear()


def _post(client, payload: dict, *, event: str = "pull_request", secret: str = SECRET):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": _sig(body, secret),
        "Content-Type": "application/json",
    }
    return client.post("/webhooks/github", content=body, headers=headers)


def test_endpoint_rejects_invalid_signature(gh_client) -> None:
    response = gh_client.post(
        "/webhooks/github",
        content=b'{"action":"closed"}',
        headers={"X-Hub-Signature-256": "sha256=deadbeef", "X-GitHub-Event": "pull_request"},
    )

    assert response.status_code == 401
    assert response.json()["reason"] == "invalid_signature"
    assert gh_client.app.state.recording_sync_service.calls == []


def test_endpoint_rejects_unsigned_request(gh_client) -> None:
    assert gh_client.post("/webhooks/github", json=_pr_payload()).status_code == 401


def test_non_pull_request_event_ignored(gh_client) -> None:
    response = _post(gh_client, {"zen": "hello"}, event="ping")

    assert response.status_code == 202
    assert response.json()["reason"] == "event_ignored"
    assert gh_client.app.state.recording_sync_service.calls == []


def test_action_not_closed_ignored(gh_client) -> None:
    response = _post(gh_client, _pr_payload(action="opened"))

    assert response.status_code == 202
    assert gh_client.app.state.recording_sync_service.calls == []


def test_closed_without_merge_ignored(gh_client) -> None:
    """`action: closed` fires for a plain close too — only `merged: true` counts."""
    payload = _pr_payload()
    payload["pull_request"]["merged"] = False

    response = _post(gh_client, payload)

    assert response.status_code == 202
    assert gh_client.app.state.recording_sync_service.calls == []


def test_wrong_base_branch_ignored(gh_client) -> None:
    payload = _pr_payload()
    payload["pull_request"]["base"]["ref"] = "staging"

    response = _post(gh_client, payload)

    assert response.status_code == 202
    assert gh_client.app.state.recording_sync_service.calls == []


def test_repo_not_allowlisted_ignored(gh_client) -> None:
    payload = _pr_payload(repository={"name": "other-repo", "owner": {"login": "acme"}})

    response = _post(gh_client, payload)

    assert response.status_code == 202
    assert gh_client.app.state.recording_sync_service.calls == []


def test_invalid_json_payload_rejected(gh_client) -> None:
    body = b"not json"
    response = gh_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sig(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_payload"


def test_happy_path_calls_the_sync_service(gh_client) -> None:
    response = _post(gh_client, _pr_payload())

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    calls = gh_client.app.state.recording_sync_service.calls
    assert len(calls) == 1
    assert calls[0].slug == "acme/api#431"


def test_disabled_feature_never_calls_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with a perfectly valid signature, the flag being off means nothing is parsed or called."""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GITHUB_MERGE_SYNC_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:
            recording = RecordingSyncService()
            test_client.app.state.github_merge_sync_service = recording

            response = _post(test_client, _pr_payload())

            assert response.status_code == 202
            assert response.json()["reason"] == "sync_disabled"
            assert recording.calls == []
    finally:
        get_settings.cache_clear()
