"""Small HTTP getter with retries. Standard library only."""

from __future__ import annotations

import time
import urllib.error
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (compatible; market-vitals-feed/1.0; "
    "+https://github.com/rrodrigoblanco/market-vitals-feed)"
)


class FetchError(RuntimeError):
    pass


def fetch_bytes(
    url: str,
    *,
    timeout: float = 40,
    attempts: int = 4,
    redact: str | None = None,
) -> bytes:
    """GET `url`, retrying on network errors and HTTP 429/500/502/503/504."""
    last_error: Exception | None = None
    delay = 1.0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            retryable = exc.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == attempts:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == attempts:
                break
        time.sleep(delay)
        delay *= 2
    message = f"GET failed after {attempts} tries: {last_error}"
    if redact:
        message = message.replace(redact, "REDACTED")
    raise FetchError(message) from last_error
