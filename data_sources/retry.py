"""
Shared retry utility for all data-source clients.

Retries only on transient network/SSL errors (connection refused, timeout,
SSL handshake failure). Auth errors, 4xx responses, and parse errors are
NOT retried — they indicate a programming or config problem, not a blip.
"""
import time
import logging
from typing import Callable, TypeVar

import httpx
import requests
import urllib3

log = logging.getLogger(__name__)

T = TypeVar("T")

# Errors that are safe to retry — all indicate a transient network condition.
_RETRYABLE = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.SSLError,
    urllib3.exceptions.ProtocolError,
    urllib3.exceptions.SSLError,
    ConnectionError,
    TimeoutError,
    OSError,
)


def with_retry(
    fn: Callable[[], T],
    *,
    label: str = "",
    max_retries: int = 3,
    initial_delay: float = 5.0,
) -> T:
    """Call fn(), retrying up to max_retries times on transient errors.

    Delay schedule: initial_delay → initial_delay*2 → initial_delay*4 …
    (5 s → 10 s → 20 s with defaults).

    Raises the last exception if all attempts are exhausted.
    """
    delay = initial_delay
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_exc = exc
            if attempt == max_retries:
                break
            log.warning(
                "[%s] attempt %d/%d failed: HTTP %s. Retrying in %.0f s …",
                label or fn.__name__,
                attempt + 1,
                max_retries,
                exc.response.status_code,
                delay,
            )
            time.sleep(delay)
            delay *= 2
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            log.warning(
                "[%s] attempt %d/%d failed: %s. Retrying in %.0f s …",
                label or fn.__name__,
                attempt + 1,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)
            delay *= 2

    raise last_exc  # type: ignore[misc]
