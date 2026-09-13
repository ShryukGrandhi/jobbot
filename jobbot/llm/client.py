"""LLM access layer.

Two interchangeable backends:

  meridian  -- local proxy (127.0.0.1:3456) bridging the Claude Agent SDK to the
               Anthropic wire format, billed against a Claude Max subscription.
  anthropic -- api.anthropic.com directly, billed per token via an API key.

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
from dotenv import load_dotenv
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
    for line in p.read_text(encoding="utf-8").splitlines():
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

# Per-read timeout on the API socket. This was 600s: a stalled connection on a
# 2,500-token knockout scan sat in SSLSocket.read for ten minutes before the
# retry logic got a look at it -- with the browser tab open and the event loop
# blocked the whole time. Streaming resets the clock on every chunk, so long
# generations are unaffected; this only bounds silence.
LLM_TIMEOUT_S = 120.0

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
        load_dotenv()   # idempotent; never overrides a real env var
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
                timeout=LLM_TIMEOUT_S,   # NOTE: a float, not httpx.Timeout -- the SDK
                                         # uses httpx2 and mishandles an httpx.Timeout,
                                         # which surfaces as an instant 'Connection error'.
                max_retries=0,   # tenacity owns retries
            )
        elif self.provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not key or key.endswith("..."):   # .env.example ships "sk-ant-..."
                raise LLMError("JOBBOT_LLM_PROVIDER=anthropic requires a real "
                               "ANTHROPIC_API_KEY in .env (or JOBBOT_LLM_PROVIDER=gemini)")
            self.client = Anthropic(
                api_key=key,
                timeout=LLM_TIMEOUT_S,
                max_retries=0,
            )
        elif self.provider == "gemini":
            # Gemini as the primary, not just the fallback. Same adapter,
            # same LLMResponse shape; the Anthropic client is simply never
            # built, so no ANTHROPIC_API_KEY is needed.
            if not os.environ.get("GEMINI_API_KEY"):
                raise LLMError("JOBBOT_LLM_PROVIDER=gemini requires GEMINI_API_KEY")
            self.client = None
            self.model = os.environ.get("JOBBOT_GEMINI_MODEL", "gemini-2.5-pro")
            self.fallback = "none"
            self.fallback_active = True    # every call routes through _call_fallback
        else:
            raise LLMError(f"unknown provider {self.provider!r} "
                           "(use 'anthropic', 'gemini' or 'meridian')")

        log.info("llm.init", provider=self.provider, model=self.model)

    @property
    def can_fall_back(self) -> bool:
        """Only fail over to a backend that is actually usable.

        `JOBBOT_LLM_FALLBACK=gemini` is the shipped default while GEMINI_API_KEY
        is empty in .env.example. Switching to it then raises out of `call()` on
        the first attempt and, because the switch is sticky, kills every later
        call in the run -- strictly worse than retrying the primary.
        """
        return self.fallback == "gemini" and bool(os.environ.get("GEMINI_API_KEY"))

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
        stop=stop_after_attempt(8),   # ~5 min of backoff; a 503 storm outlasts 5 tries
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
            if _is_quota_exhausted(exc) and self.can_fall_back:
                log.warning("llm.quota_exhausted_failing_over", error=str(exc)[:200])
                self.fallback_active = True
                return self._call_fallback(system, blocks, tool, kwargs["max_tokens"])
            if _is_transient(exc):
                self._primary_failures += 1
                # Repeated transient failure is indistinguishable from an outage.
                # Switch rather than burn the retry budget on a dead backend.
                if self._primary_failures >= 3 and self.can_fall_back:
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
        if tool is not None and res.get("tool_input") is None:
            # A forced tool call that came back as prose (or nothing) is a
            # blip -- Gemini does this on a fraction of vision requests --
            # and a re-ask costs seconds, whereas surfacing it cost a live
            # run a full navigate-and-refill pass.
            raise TransientLLMError(
                f"gemini returned no tool call for {tool.get('name')}; "
                f"text was: {str(res.get('text', ''))[:200]!r}")
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


# Anthropic error `type`s that clear on their own. Checked against the body
# before the status code, because a mid-stream error event arrives on a 200
# response: the SDK wraps it in a generic APIStatusError whose status_code is
# that 200, so the status says nothing and the body says everything.
_TRANSIENT_BODY = ("overloaded_error", "rate_limit_error", "api_error")


def _is_transient(exc: Exception) -> bool:
    blob = str(exc).lower()
    if any(t in blob for t in _TRANSIENT_BODY):
        return True
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
    """Subscription window exhausted -- retrying will not help, only waiting.

    Deliberately does NOT match a plain `rate_limit_error`. Anthropic returns
    that type for an ordinary per-minute 429, which clears in seconds; treating
    it as exhaustion made the first burst of concurrency fail the whole run over
    to the fallback permanently.
    """
    blob = str(exc).lower()
    return any(s in blob for s in (
        "out of extra usage", "usage limit", "quota exceeded",
        "exceeded your current quota",
    ))
