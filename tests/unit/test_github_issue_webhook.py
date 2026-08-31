"""Webhook dispatch for GitHub Issues / security alerts -> Linear (CLAUDE.md §1c).

Mirrors `tests/unit/test_github_webhook.py`'s structure (the §1b tests) —
same signature-verification coverage is not repeated here since it's
event-type-agnostic and already covered there; this file is about the four
new event types' dispatch and payload extraction.
"""

import hashlib
import hmac
import json

import pytest

SECRET = "test-github-webhook-secret"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"


class RecordingSyncService:
    def __init__(self) -> None:
        self.issue_calls = []
        self.alert_calls = []

    async def handle_issue_event(self, event) -> None:
        self.issue_calls.append(event)

    async def handle_security_alert(self, event) -> None:
        self.alert_calls.append(event)


def _issue_payload(**overrides) -> dict:
    payload = {
        "action": "opened",
        "repository": {"name": "api", "owner": {"login": "acme"}},
        "issue": {
            "number": 42,
            "title": "Authentication fails for new users",
            "body": "Steps to reproduce...",
            "state": "open",
            "labels": [{"name": "bug"}],
            "user": {"login": "alice"},
            "html_url": "https://github.com/acme/api/issues/42",
        },
    }
    payload.update(overrides)
    return payload


def _code_scanning_payload(**overrides) -> dict:
    payload = {
        "action": "created",
        "repository": {"name": "api", "owner": {"login": "acme"}},
        "alert": {
            "number": 31,
            "state": "open",
            "html_url": "https://github.com/acme/api/security/code-scanning/31",
            "rule": {"id": "sql-injection", "description": "SQL Injection", "severity": "error"},
            "most_recent_instance": {
                "ref": "refs/heads/main",
                "commit_sha": "abc123",
                "location": {"path": "src/db.py", "start_line": 42},
                "message": {"text": "User input flows into a query"},
            },
        },
    }
    payload.update(overrides)
    return payload


def _dependabot_payload(**overrides) -> dict:
    payload = {
        "action": "created",
        "repository": {"name": "api", "owner": {"login": "acme"}},
        "alert": {
            "number": 44,
            "state": "open",
            "html_url": "https://github.com/acme/api/security/dependabot/44",
            "dependency": {"package": {"name": "requests", "ecosystem": "pip"}},
            "security_advisory": {"summary": "ReDoS in requests", "severity": "high"},
            "security_vulnerability": {
                "vulnerable_version_range": "< 2.0.0",
                "first_patched_version": {"identifier": "2.0.1"},
            },
        },
    }
    payload.update(overrides)
    return payload


def _secret_scanning_payload(**overrides) -> dict:
    payload = {
        "action": "created",
        "repository": {"name": "api", "owner": {"login": "acme"}},
        "alert": {
            "number": 12,
            "state": "open",
            "html_url": "https://github.com/acme/api/security/secret-scanning/12",
            "secret_type": "aws_access_key_id",
            "secret_type_display_name": "AWS Access Key ID",
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def gh_client(monkeypatch: pytest.MonkeyPatch):
    """A fresh app instance with the GitHub issue/vulnerability sync feature enabled."""
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_ISSUE_SYNC_ENABLED", "true")
    monkeypatch.setenv("GITHUB_REPO_ALLOWLIST", "acme/api")
    monkeypatch.setenv("LINEAR_SYNC_TEAM_KEY", "GIT")
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:
            recording = RecordingSyncService()
            test_client.app.state.github_issue_sync_service = recording
            test_client.app.state.recording_sync_service = recording
            yield test_client
    finally:
        get_settings.cache_clear()


def _post(client, payload: dict, *, event: str, secret: str = SECRET):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": _sig(body, secret),
        "Content-Type": "application/json",
    }
    return client.post("/webhooks/github", content=body, headers=headers)


# --- GitHub Issues -----------------------------------------------------------


def test_issue_opened_calls_the_sync_service(gh_client) -> None:
    response = _post(gh_client, _issue_payload(), event="issues")

    assert response.status_code == 202
    calls = gh_client.app.state.recording_sync_service.issue_calls
    assert len(calls) == 1
    assert calls[0].number == 42
    assert calls[0].labels == ["bug"]
    assert calls[0].author_login == "alice"


def test_issue_edited_also_syncs(gh_client) -> None:
    """No action filtering for the sync events — every delivery is a create-or-update."""
    response = _post(gh_client, _issue_payload(action="edited"), event="issues")

    assert response.status_code == 202
    assert len(gh_client.app.state.recording_sync_service.issue_calls) == 1


def test_issue_repo_not_allowlisted_ignored(gh_client) -> None:
    payload = _issue_payload(repository={"name": "other-repo", "owner": {"login": "acme"}})

    response = _post(gh_client, payload, event="issues")

    assert response.status_code == 202
    assert gh_client.app.state.recording_sync_service.issue_calls == []


# --- Security alerts -----------------------------------------------------------


def test_code_scanning_alert_calls_the_sync_service(gh_client) -> None:
    response = _post(gh_client, _code_scanning_payload(), event="code_scanning_alert")

    assert response.status_code == 202
    calls = gh_client.app.state.recording_sync_service.alert_calls
    assert len(calls) == 1
    assert calls[0].source_type == "code_scan"
    assert calls[0].commit_sha == "abc123"
    assert calls[0].details["rule_description"] == "SQL Injection"
    assert calls[0].details["file"] == "src/db.py"


def test_dependabot_alert_calls_the_sync_service(gh_client) -> None:
    response = _post(gh_client, _dependabot_payload(), event="dependabot_alert")

    assert response.status_code == 202
    calls = gh_client.app.state.recording_sync_service.alert_calls
    assert len(calls) == 1
    assert calls[0].source_type == "dependabot"
    assert calls[0].commit_sha is None
    assert calls[0].details["package"] == "requests"
    assert calls[0].details["fixed_version"] == "2.0.1"


def test_secret_scanning_alert_calls_the_sync_service(gh_client) -> None:
    response = _post(gh_client, _secret_scanning_payload(), event="secret_scanning_alert")

    assert response.status_code == 202
    calls = gh_client.app.state.recording_sync_service.alert_calls
    assert len(calls) == 1
    assert calls[0].source_type == "secret_scan"
    assert calls[0].details["secret_type"] == "AWS Access Key ID"


def test_alert_repo_not_allowlisted_ignored(gh_client) -> None:
    payload = _code_scanning_payload(repository={"name": "other-repo", "owner": {"login": "acme"}})

    response = _post(gh_client, payload, event="code_scanning_alert")

    assert response.status_code == 202
    assert gh_client.app.state.recording_sync_service.alert_calls == []


# --- Other GitHub events are never synced -------------------------------------


_pr_opened_payload = {
    "action": "opened",
    "repository": {"name": "api", "owner": {"login": "acme"}},
}


@pytest.mark.parametrize(
    "event,payload_fn",
    [
        ("pull_request", lambda: _pr_opened_payload),
        ("check_run", lambda: {"action": "completed"}),
        ("workflow_run", lambda: {"action": "completed", "workflow_run": {"conclusion": "success"}}),
        ("status", lambda: {"state": "success"}),
    ],
)
def test_non_issue_sync_events_never_reach_the_service(gh_client, event, payload_fn) -> None:
    response = _post(gh_client, payload_fn(), event=event)

    assert response.status_code == 202
    recording = gh_client.app.state.recording_sync_service
    assert recording.issue_calls == []
    assert recording.alert_calls == []


# --- Feature gate --------------------------------------------------------------


def test_disabled_feature_never_calls_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("GITHUB_ISSUE_SYNC_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:
            recording = RecordingSyncService()
            test_client.app.state.github_issue_sync_service = recording

            response = _post(test_client, _issue_payload(), event="issues")

            assert response.status_code == 202
            assert response.json()["reason"] == "sync_disabled"
            assert recording.issue_calls == []
    finally:
        get_settings.cache_clear()


def test_no_github_credentials_is_a_graceful_no_op(gh_client) -> None:
    """`github_issue_sync_service` is None when no GitHub credentials are configured
    (it needs GitHub reads) — the dispatch must not crash on that."""
    gh_client.app.state.github_issue_sync_service = None

    response = _post(gh_client, _issue_payload(), event="issues")

    assert response.status_code == 202
