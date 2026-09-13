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


def test_gemini_schema_sanitizer_keeps_a_property_named_title():
    """A resume schema has a job `title` field; Gemini must still see it.

    The sanitizer strips schema keywords Gemini rejects ("title", "default",
    ...). It applied that to the *property map* too, so `experience.items`
    lost its `title` property while `required` still listed it, and the
    first tailoring call of a live run failed with
    "required[1]: property is not defined".
    """
    from jobbot.llm.gemini import sanitize_schema

    schema = {
        "type": "object",
        "title": "Resume",                       # keyword: must go
        "properties": {
            "title": {"type": "string", "title": "Headline"},   # name: must stay
            "default": {"type": ["string", "null"]},           # name: must stay
            "experience": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"company": {"type": "string"},
                                   "title": {"type": "string"}},
                    "required": ["company", "title"],
                },
            },
        },
        "required": ["title", "experience"],
    }
    out = sanitize_schema(schema)
    assert "title" not in out                    # the keyword
    assert set(out["properties"]) == {"title", "default", "experience"}
    assert "title" not in out["properties"]["title"]
    assert out["properties"]["default"] == {"type": "STRING", "nullable": True}
    items = out["properties"]["experience"]["items"]
    assert set(items["properties"]) >= set(items["required"])
    assert "minItems" not in out["properties"]["experience"]
