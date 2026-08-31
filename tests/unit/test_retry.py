"""`retry_async`: bounded exponential backoff for transient failures."""

import pytest

from app.core.retry import retry_async


class _Flaky:
    """Raises `error` for the first `fail_count` calls, then returns `result`."""

    def __init__(self, error: Exception, fail_count: int, result: str = "ok") -> None:
        self._error = error
        self._fail_count = fail_count
        self._result = result
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self._fail_count:
            raise self._error
        return self._result


class _RecordingSleep:
    """A fake `sleep` that records delays instead of actually waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def test_succeeds_on_first_try_without_sleeping() -> None:
    sleep = _RecordingSleep()
    func = _Flaky(ValueError("boom"), fail_count=0)

    result = await retry_async(func, is_retryable=lambda exc: True, sleep=sleep)

    assert result == "ok"
    assert func.calls == 1
    assert sleep.delays == []


async def test_succeeds_after_retrying_and_backs_off_exponentially() -> None:
    sleep = _RecordingSleep()
    func = _Flaky(ValueError("boom"), fail_count=2)

    result = await retry_async(
        func, is_retryable=lambda exc: True, base_delay_s=0.1, max_delay_s=10.0, sleep=sleep
    )

    assert result == "ok"
    assert func.calls == 3
    assert sleep.delays == [0.1, 0.2]  # base * 2**0, base * 2**1


async def test_backoff_is_capped_at_max_delay() -> None:
    sleep = _RecordingSleep()
    func = _Flaky(ValueError("boom"), fail_count=3)

    await retry_async(
        func, is_retryable=lambda exc: True, max_attempts=4, base_delay_s=1.0, max_delay_s=1.5, sleep=sleep
    )

    assert sleep.delays == [1.0, 1.5, 1.5]  # 1*2**0, min(1*2**1, 1.5), min(1*2**2, 1.5)


async def test_exhausts_attempts_and_reraises_the_last_exception() -> None:
    sleep = _RecordingSleep()
    error = ValueError("always fails")
    func = _Flaky(error, fail_count=99)

    with pytest.raises(ValueError, match="always fails"):
        await retry_async(func, is_retryable=lambda exc: True, max_attempts=3, sleep=sleep)

    assert func.calls == 3
    assert len(sleep.delays) == 2  # slept between attempts 1->2 and 2->3, not after the last


async def test_non_retryable_exception_raises_immediately_without_sleeping() -> None:
    sleep = _RecordingSleep()
    func = _Flaky(ValueError("permanent"), fail_count=99)

    with pytest.raises(ValueError, match="permanent"):
        await retry_async(func, is_retryable=lambda exc: False, sleep=sleep)

    assert func.calls == 1
    assert sleep.delays == []


async def test_is_retryable_receives_the_actual_exception() -> None:
    class _SpecialError(Exception):
        pass

    func = _Flaky(_SpecialError("special"), fail_count=1)
    seen: list[Exception] = []

    def _predicate(exc: Exception) -> bool:
        seen.append(exc)
        return isinstance(exc, _SpecialError)

    result = await retry_async(func, is_retryable=_predicate, sleep=_RecordingSleep())

    assert result == "ok"
    assert len(seen) == 1
    assert isinstance(seen[0], _SpecialError)
