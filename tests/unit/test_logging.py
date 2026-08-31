"""Structured logging output and secret redaction."""

import json
import logging

from app.core.logging import JSONFormatter


def _render(record: logging.LogRecord) -> dict:
    return json.loads(JSONFormatter().format(record))


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=None,
        exc_info=None,
    )
    record.__dict__.update(extra)
    return record


def test_output_is_single_line_json() -> None:
    rendered = JSONFormatter().format(_record())

    assert "\n" not in rendered
    assert json.loads(rendered)["message"] == "event"


def test_standard_fields_present() -> None:
    payload = _render(_record())

    assert set(payload) >= {"timestamp", "level", "logger", "message"}
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"


def test_extra_fields_are_nested() -> None:
    payload = _render(_record(review_id="r-1", sandbox_id="sb-9"))

    assert payload["extra"] == {"review_id": "r-1", "sandbox_id": "sb-9"}


def test_no_extra_key_when_nothing_extra() -> None:
    assert "extra" not in _render(_record())


def test_secrets_are_redacted() -> None:
    """Credential-shaped field names must never render their value."""
    payload = _render(
        _record(
            openai_api_key="sk-real-secret",
            github_private_key="-----BEGIN KEY-----",
            linear_webhook_secret="hunter2",
            authorization="Bearer abc",
            review_id="r-1",
        )
    )

    serialized = json.dumps(payload)
    for leaked in ("sk-real-secret", "BEGIN KEY", "hunter2", "Bearer abc"):
        assert leaked not in serialized

    # Non-secret fields still come through.
    assert payload["extra"]["review_id"] == "r-1"


def test_redaction_is_case_insensitive() -> None:
    payload = _render(_record(API_KEY="secret-value", Token="another-secret"))

    assert "secret-value" not in json.dumps(payload)
    assert "another-secret" not in json.dumps(payload)


def test_exception_is_captured() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
        payload = _render(record)

    assert "ValueError" in payload["exception"]
