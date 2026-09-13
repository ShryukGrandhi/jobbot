"""Which backend errors are worth retrying.

Both guards below fail on the unfixed classifier, and neither failure is
visible at runtime: the run keeps going, on the wrong backend or not at all.
"""

from __future__ import annotations

import pytest

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


def test_gemini_503_is_transient_and_retried_not_fatal(monkeypatch):
    """A 503 "high demand" at checkpoint 2 must not throw away a filled form.

    call_gemini raised a plain RuntimeError, which bypassed the client's
    retry policy and surfaced as a crash mid-application.
    """
    import httpx
    import pytest

    from jobbot.llm import gemini
    from jobbot.llm.client import TransientLLMError

    monkeypatch.setenv("GEMINI_API_KEY", "k")

    class R:
        def __init__(self, code, text): self.status_code, self.text = code, text
        def json(self): return {}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(503, "high demand, try again later"))
    with pytest.raises(TransientLLMError):
        gemini.call_gemini(system="s", blocks=[{"type": "text", "text": "x"}], tool=None, max_tokens=10)

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R(400, "bad schema"))
    with pytest.raises(RuntimeError) as ei:
        gemini.call_gemini(system="s", blocks=[{"type": "text", "text": "x"}], tool=None, max_tokens=10)
    assert not isinstance(ei.value, TransientLLMError), "a 400 is a bug, not a blip"

    def boom(*a, **k): raise httpx.ConnectTimeout("t")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(TransientLLMError):
        gemini.call_gemini(system="s", blocks=[{"type": "text", "text": "x"}], tool=None, max_tokens=10)


def test_gemini_prose_instead_of_tool_call_is_retried(monkeypatch):
    """A forced tool call answered with empty prose is a blip, not a crash."""
    from jobbot.llm import client as mod
    from jobbot.llm import gemini

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(gemini, "call_gemini", lambda **kw: {
        "text": "", "tool_input": None, "model": "gemini-2.5-pro",
        "input_tokens": 1, "output_tokens": 0})
    c = mod.LLMClient(provider="gemini")
    with pytest.raises(mod.TransientLLMError):
        c._call_fallback(None, [{"type": "text", "text": "x"}], {"name": "parse"}, 100)

    # prose was what we asked for: no tool, no error
    out = c._call_fallback(None, [{"type": "text", "text": "x"}], None, 100)
    assert out.tool_input is None
