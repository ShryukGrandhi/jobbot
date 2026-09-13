"""Gemini backend, used as a fallback when Meridian is unavailable.

Meridian rides a Claude Max subscription, so it is subject to that plan's
five-hour and seven-day windows. An unattended overnight run that exhausts the
bucket would otherwise stop dead mid-application, leaving half-filled forms in
open tabs. Failing over keeps the run alive.

The adapter maps our Anthropic-shaped calls onto Gemini's REST API and
normalizes the response back into `LLMResponse`, so nothing upstream changes.
"""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

import httpx
import structlog

log = structlog.get_logger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = os.environ.get("JOBBOT_GEMINI_MODEL", "gemini-2.5-pro")
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}

# Gemini's function-calling schema is a strict OpenAPI subset. These keywords
# are silently rejected or cause a 400.
_DROP_KEYS = {"additionalProperties", "$schema", "default", "examples",
              "minimum", "maximum", "minItems", "maxItems", "title"}
# Dict-valued keywords whose keys are user property names, never keywords.
_PROPERTY_MAPS = {"properties", "$defs", "definitions"}


def sanitize_schema(node: Any) -> Any:
    """Rewrite an Anthropic-style JSON schema into Gemini's dialect.

    The consequential transform is union types. We write `{"type": ["string",
    "null"]}` so a model can decline to answer; Gemini rejects the array form,
    so we collapse to the first non-null type and mark it nullable.
    """
    if isinstance(node, list):
        return [sanitize_schema(x) for x in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in _PROPERTY_MAPS and isinstance(v, dict):
            # The keys here are property NAMES, not schema keywords. A field
            # called "title" or "default" must survive, or `required` ends up
            # naming a property that no longer exists and Gemini returns 400.
            out[k] = {name: sanitize_schema(sub) for name, sub in v.items()}
            continue
        if k in _DROP_KEYS:
            continue
        if k == "type" and isinstance(v, list):
            non_null = [t for t in v if t != "null"]
            out["type"] = (non_null[0] if non_null else "string").upper()
            if len(non_null) < len(v):
                out["nullable"] = True
            continue
        if k == "type" and isinstance(v, str):
            out["type"] = v.upper()
            continue
        out[k] = sanitize_schema(v)
    return out


def _blocks_to_parts(blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for b in blocks:
        if b.get("type") == "text":
            parts.append({"text": b["text"]})
        elif b.get("type") == "image":
            src = b.get("source", {})
            parts.append({
                "inline_data": {
                    "mime_type": src.get("media_type", "image/png"),
                    "data": src.get("data", ""),
                }
            })
    return parts


def _system_text(system: str | list[dict[str, Any]] | None) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))


def call_gemini(
    *,
    system: str | list[dict[str, Any]] | None,
    blocks: Sequence[dict[str, Any]],
    tool: dict[str, Any] | None,
    max_tokens: int,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Return a normalized dict: {text, tool_input, input_tokens, output_tokens}."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": _blocks_to_parts(blocks)}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    sys_txt = _system_text(system)
    if sys_txt:
        body["system_instruction"] = {"parts": [{"text": sys_txt}]}

    if tool is not None:
        body["tools"] = [{
            "function_declarations": [{
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": sanitize_schema(tool["input_schema"]),
            }]
        }]
        # ANY + an explicit allowlist is Gemini's equivalent of forcing a tool.
        body["tool_config"] = {
            "function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": [tool["name"]],
            }
        }

    from jobbot.llm.client import TransientLLMError   # lazy: client imports us lazily too

    try:
        r = httpx.post(
            ENDPOINT.format(model=model),
            params={"key": key},
            json=body,
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise TransientLLMError(f"gemini network: {exc}") from exc
    if r.status_code in TRANSIENT_STATUS:
        # 503 "high demand", 429, 5xx: the call is retried with backoff by the
        # client's policy. Raising a plain error here cost a live run its
        # fully filled form at checkpoint 2.
        raise TransientLLMError(f"gemini {r.status_code}: {r.text[:400]}")
    if r.status_code != 200:
        raise RuntimeError(f"gemini {r.status_code}: {r.text[:400]}")
    data = r.json()

    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError(f"gemini returned no candidates: {json.dumps(data)[:300]}")

    text_parts: list[str] = []
    tool_input: dict[str, Any] | None = None
    for p in (cands[0].get("content") or {}).get("parts", []):
        if "text" in p:
            text_parts.append(p["text"])
        if "functionCall" in p:
            tool_input = dict(p["functionCall"].get("args") or {})

    usage = data.get("usageMetadata") or {}
    return {
        "text": "\n".join(text_parts).strip(),
        "tool_input": tool_input,
        "input_tokens": usage.get("promptTokenCount", 0) or 0,
        "output_tokens": usage.get("candidatesTokenCount", 0) or 0,
        "model": model,
    }
