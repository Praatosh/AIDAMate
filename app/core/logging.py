"""Structured JSON logging.

Emits one JSON object per line to stdout. Application-specific fields passed
via `extra={...}` are nested under an "extra" key, which is how every review
lifecycle event (see `app/core/events.py`) carries its `review_id`,
`linear_issue_id`, `sandbox_id`, `duration_ms`, and so on.

Built on the standard library — no additional logging dependency required.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attribute names present on a stock LogRecord. Anything beyond these came from
# an `extra={...}` argument and is therefore application data worth surfacing.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

_REDACTED = "***REDACTED***"

# Field names whose values must never reach the log stream, matched
# case-insensitively as substrings so `openai_api_key`, `API_KEY`, and
# `github_private_key` are all caught.
_SECRET_FIELD_MARKERS = ("secret", "token", "api_key", "apikey", "password", "private_key", "authorization")


def _is_secret_field(name: str) -> bool:
    """Whether a field name looks like it carries a credential."""
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_FIELD_MARKERS)


class JSONFormatter(logging.Formatter):
    """Renders each log record as a single-line JSON object, redacting secrets."""

    def format(self, record: logging.LogRecord) -> str:
        """Build the JSON payload for one record, including `extra` fields."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        extras = {
            key: (_REDACTED if _is_secret_field(key) else value)
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_")
        }
        if extras:
            payload["extra"] = extras

        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with a single JSON stdout handler.

    Idempotent: replaces handlers previously installed by this function rather
    than stacking duplicates across repeated calls (e.g. per test).
    """
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.handlers = [handler]


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call `setup_logging()` once at startup first."""
    return logging.getLogger(name)
