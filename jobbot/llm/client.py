"""LLM access layer.

Two interchangeable backends:

  anthropic -- api.anthropic.com directly, billed per token via an API key.
               This is the default and the supported path.
  meridian  -- a local proxy bridging a Claude subscription to the Anthropic
               wire format. It works, but Anthropic's Agent SDK docs direct
               third-party tools to API-key auth, so the account risk is yours.

Both speak the Anthropic Messages API, so the only difference is base_url/api_key.
Set JOBBOT_LLM_PROVIDER to switch.
"""

from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx
import structlog
from anthropic import Anthropic
from PIL import Image
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)


def _load_env(path: str = ".env") -> None:
    """Read secrets from a gitignored .env without clobbering real env vars."""
    p = Path(path)
    if not p.exists():
        p = Path(__file__).resolve().parents[2] / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

MERIDIAN_URL = os.environ.get("MERIDIAN_URL", "http://127.0.0.1:3456")

# Claude bills images in 28x28 patches: ceil(w/28) * ceil(h/28) visual tokens.
# The high-res tier caps the long edge at 2576px (4784 tokens); the standard tier
# at 1568px (1568 tokens). Full-page application screenshots are extremely tall,
# so we downscale to a budget rather than paying for a 10000px-tall strip.
# Requests above this budget are streamed; long non-streaming calls time out.
STREAM_THRESHOLD_TOKENS = 6000

HIRES_LONG_EDGE = 2576
STANDARD_LONG_EDGE = 1568


class LLMError(RuntimeError):
    pass


class TransientLLMError(LLMError):
    """Overload / rate limit / network blip -- worth retrying."""


@dataclass
class LLMResponse:
    text: str
    tool_input: dict[str, Any] | None
    thinking: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    raw: Any = field(repr=False, default=None)

    def require_tool(self) -> dict[str, Any]:
        if self.tool_input is None:
            raise LLMError(
                f"model returned no structured output; text was: {self.text[:500]!r}"
            )
        return self.tool_input


def encode_image(
    path: str | Path,
    *,
    long_edge: int = HIRES_LONG_EDGE,
    max_bytes: int = 4_500_000,
) -> dict[str, Any]:
    """Load an image, downscale to a token budget, return an Anthropic image block.

    Full-page screenshots of application forms are routinely 1200x9000. Sending
    that raw is both expensive and worse for the model (tiny text after the
    server-side resize). We cap the long edge and re-encode.
    """
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    w, h = img.size
    scale = min(1.0, long_edge / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()

    # PNG of a dense form page can still be large; fall back to JPEG if so.
    media_type = "image/png"
    if len(data) > max_bytes:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82, optimize=True)
        data = buf.getvalue()
        media_type = "image/jpeg"

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode(),
        },
    }


def slice_tall_image(
    path: str | Path,
    out_dir: str | Path,
    *,
    max_aspect: float = 3.0,
    overlap: int = 120,
) -> list[Path]:
    """Split a very tall full-page screenshot into overlapping vertical slices.

    A 1200x12000 page downscaled to fit 2576px on the long edge leaves the text
    illegible. Slicing keeps each piece near-native resolution. Overlap prevents
    a form field from being cut in half across the seam.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(path)
    w, h = img.size

    if h <= w * max_aspect:
        dest = out_dir / f"{Path(path).stem}_0.png"
        img.save(dest)
        return [dest]

    slice_h = int(w * max_aspect)
    step = slice_h - overlap
    slices: list[Path] = []
    top = 0
    idx = 0
    while top < h:
        bottom = min(h, top + slice_h)
        dest = out_dir / f"{Path(path).stem}_{idx}.png"
        img.crop((0, top, w, bottom)).save(dest)
        slices.append(dest)
        if bottom >= h:
            break
        top += step
        idx += 1
    return slices


class LLMClient:
    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int = 8000,
        fallback: str | None = None,
    ) -> None:
        self.provider = (provider or os.environ.get("JOBBOT_LLM_PROVIDER", "anthropic")).lower()
        self.model = model or os.environ.get("JOBBOT_LLM_MODEL", "claude-opus-5")
        self.max_tokens = max_tokens
        # Meridian rides a subscription with five-hour and seven-day windows. An
        # unattended run that exhausts one would otherwise stop mid-application,
        # leaving half-filled forms behind. Failing over keeps the run alive.
        self.fallback = (fallback or os.environ.get("JOBBOT_LLM_FALLBACK", "gemini")).lower()
        self.fallback_active = False
        self._primary_failures = 0

        if self.provider == "meridian":
            # Meridian authenticates through the Claude Agent SDK, not an API key;
            # the SDK requires the field to be set, but any value works.
            self.client = Anthropic(
                base_url=MERIDIAN_URL,
                api_key=os.environ.get("ANTHROPIC_API_KEY", "meridian-placeholder"),
                timeout=600.0,   # NOTE: a float, not httpx.Timeout -- the SDK
                                 # uses httpx2 and mishandles an httpx.Timeout,
                                 # which surfaces as an instant 'Connection error'.
                max_retries=0,   # tenacity owns retries
            )
        elif self.provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise LLMError("JOBBOT_LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
            self.client = Anthropic(
                api_key=key,
                timeout=600.0,
                max_retries=0,
            )
        else:
            raise LLMError(f"unknown provider {self.provider!r} (use 'meridian' or 'anthropic')")

        log.info("llm.init", provider=self.provider, model=self.model)

    # -- health / quota ----------------------------------------------------

    def health(self) -> dict[str, Any]:
        if self.provider != "meridian":
            return {"status": "n/a", "provider": self.provider}
        try:
            r = httpx.get(f"{MERIDIAN_URL}/health", timeout=10)
            return r.json()
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"meridian not reachable at {MERIDIAN_URL}: {exc}") from exc

    def quota(self) -> dict[str, Any] | None:
        """Remaining Max-plan quota, when the backend exposes it."""
        if self.provider != "meridian":
            return None
        try:
            r = httpx.get(f"{MERIDIAN_URL}/v1/usage/quota", timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("llm.quota_unavailable", error=str(exc))
        return None

    # -- core call ---------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(TransientLLMError),
        wait=wait_exponential(multiplier=3, min=3, max=120),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def call(
        self,
        *,
        system: str | list[dict[str, Any]] | None = None,
        blocks: Sequence[dict[str, Any]],
        tool: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """One Messages API turn.

        `tool` is a JSON-schema tool definition; when given, the model is forced
        to call it, which is how we get schema-valid structured output instead of
        parsing prose.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [{"role": "user", "content": list(blocks)}],
        }
        if system is not None:
            kwargs["system"] = system
        if tool is not None:
            kwargs["tools"] = [tool]
            kwargs["tool_choice"] = {"type": "tool", "name": tool["name"]}

        if self.fallback_active:
            return self._call_fallback(system, blocks, tool, kwargs["max_tokens"])

        # Long non-streaming requests are cut off by an upstream timeout -- a
        # large generation returns a few tokens and then dies. Anthropic's own
        # guidance is to stream anything long, so switch automatically rather
        # than making every caller remember to.
        use_stream = kwargs["max_tokens"] >= STREAM_THRESHOLD_TOKENS

        try:
            if use_stream:
                with self.client.messages.stream(**kwargs) as stream:
                    msg = stream.get_final_message()
            else:
                msg = self.client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_quota_exhausted(exc) and self.fallback == "gemini":
                log.warning("llm.quota_exhausted_failing_over", error=str(exc)[:200])
                self.fallback_active = True
                return self._call_fallback(system, blocks, tool, kwargs["max_tokens"])
            if _is_transient(exc):
                self._primary_failures += 1
                # Repeated transient failure is indistinguishable from an outage.
                # Switch rather than burn the retry budget on a dead backend.
                if self._primary_failures >= 3 and self.fallback == "gemini":
                    log.warning("llm.primary_unhealthy_failing_over",
                                failures=self._primary_failures)
                    self.fallback_active = True
                    return self._call_fallback(system, blocks, tool, kwargs["max_tokens"])
                log.warning("llm.transient", error=str(exc)[:300])
                raise TransientLLMError(str(exc)) from exc
            raise LLMError(str(exc)) from exc
        self._primary_failures = 0

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_input: dict[str, Any] | None = None

        for blk in msg.content:
            btype = getattr(blk, "type", None)
            if btype == "text":
                text_parts.append(blk.text)
            elif btype == "thinking":
                thinking_parts.append(getattr(blk, "thinking", "") or "")
            elif btype == "tool_use":
                tool_input = dict(blk.input)

        u = msg.usage
        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_input=tool_input,
            thinking="\n".join(thinking_parts).strip(),
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            raw=msg,
        )

    def _call_fallback(
        self,
        system: str | list[dict[str, Any]] | None,
        blocks: Sequence[dict[str, Any]],
        tool: dict[str, Any] | None,
        max_tokens: int,
    ) -> LLMResponse:
        from jobbot.llm.gemini import call_gemini

        res = call_gemini(system=system, blocks=blocks, tool=tool, max_tokens=max_tokens)
        log.info("llm.fallback_used", model=res["model"])
        return LLMResponse(
            text=res["text"], tool_input=res["tool_input"], thinking="",
            input_tokens=res["input_tokens"], output_tokens=res["output_tokens"],
            cache_read_tokens=0, cache_write_tokens=0,
        )

    # -- convenience -------------------------------------------------------

    def vision(
        self,
        *,
        system: str | list[dict[str, Any]] | None,
        prompt: str,
        images: Iterable[str | Path],
        tool: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        long_edge: int = HIRES_LONG_EDGE,
    ) -> LLMResponse:
        blocks: list[dict[str, Any]] = [encode_image(p, long_edge=long_edge) for p in images]
        blocks.append({"type": "text", "text": prompt})
        return self.call(system=system, blocks=blocks, tool=tool, max_tokens=max_tokens)


def cached_system(text: str) -> list[dict[str, Any]]:
    """System prompt marked for caching.

    The candidate profile is large, identical across every call, and re-sent for
    each of the three per-application checkpoints. Caching it is the difference
    between a sustainable run and burning the rate limit on redundant prefix.
    """
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "APIStatusError",
    }:
        status = getattr(exc, "status_code", None)
        if status is not None and status not in (408, 409, 429, 500, 502, 503, 504, 529):
            return False
        return True
    blob = str(exc).lower()
    return any(
        s in blob
        for s in ("overloaded", "rate limit", "timeout", "connection", "502", "503", "529")
    )


def _is_quota_exhausted(exc: Exception) -> bool:
    """Subscription window exhausted -- retrying will not help, only waiting."""
    blob = str(exc).lower()
    return any(s in blob for s in (
        "out of extra usage", "usage limit", "quota exceeded",
        "rate_limit_error", "exceeded your current quota",
    ))
