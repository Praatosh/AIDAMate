"""Bounded exponential-backoff retry for transient external-service failures.

Deliberately not applied to the OpenAI Agents SDK path (review_agent.py) —
the underlying `openai` client already retries transient failures
internally. This exists for the two HTTP clients this codebase owns
directly and that have never had retry: GitHubService and
LinearGraphQLClient.
"""

import asyncio
from collections.abc import Awaitable, Callable


async def retry_async[T](
    func: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[Exception], bool],
    max_attempts: int = 3,
    base_delay_s: float = 0.2,
    max_delay_s: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Call `func()`, retrying up to `max_attempts` total tries when the
    raised exception satisfies `is_retryable`. Re-raises immediately for
    anything `is_retryable` rejects, and re-raises the last exception once
    attempts are exhausted. Deterministic exponential backoff (no jitter —
    this app's scale doesn't need thundering-herd protection, and
    determinism keeps tests simple), capped at `max_delay_s`.

    `sleep` defaults to `None` rather than binding `asyncio.sleep` directly
    as the parameter default — a default value is captured once at import
    time, which would make it immune to tests monkeypatching `asyncio.sleep`
    to keep the suite fast. Resolving it inside the function body instead
    means a patched `asyncio.sleep` is picked up on every call.
    """
    _sleep = sleep or asyncio.sleep
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await func()
        except Exception as exc:
            if not is_retryable(exc) or attempt == max_attempts - 1:
                raise
            last_exc = exc
            await _sleep(min(base_delay_s * (2**attempt), max_delay_s))
    raise last_exc  # pragma: no cover - unreachable, satisfies type checkers
