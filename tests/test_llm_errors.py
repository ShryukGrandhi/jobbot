"""Which backend errors are worth retrying.

Both guards below fail on the unfixed classifier, and neither failure is
visible at runtime: the run keeps going, on the wrong backend or not at all.
"""

from __future__ import annotations

from jobbot.llm.client import _is_quota_exhausted, _is_transient


def test_plain_429_is_transient_not_exhaustion() -> None:
    """An ordinary per-minute 429 must stay retryable.

    Matching `rate_limit_error` as exhaustion flipped `fallback_active` on the
    first burst of concurrency, and the flip is sticky for the whole run.
    """
    exc = Exception(
        "Error code: 429 - {'type': 'error', 'error': "
        "{'type': 'rate_limit_error', 'message': 'rate limit exceeded'}}"
    )
    assert not _is_quota_exhausted(exc)
    assert _is_transient(exc)


def test_real_exhaustion_still_detected() -> None:
    assert _is_quota_exhausted(Exception("You are out of extra usage credits"))
    assert _is_quota_exhausted(Exception("Quota exceeded for this window"))
