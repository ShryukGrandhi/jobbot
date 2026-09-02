"""Screenshot capture tuned for a vision model reading application forms.

`full_page=True` is not the right primitive here, for three documented reasons:

  * Chromium rasters into a surface capped by max texture size (commonly
    16,384px). Taller pages truncate or tile -- sometimes silently repeating a
    band of the page, which reads to a model as a real repeated section.
  * Playwright does not scroll before capturing, so lazy-loaded and
    intersection-observed content is simply absent.
  * `position: fixed` headers render once into an expanded surface and land in
    the wrong place, or repeat.

So we scroll to force render, then take viewport-sized tiles at known offsets.
Each tile stays near native resolution, which is what keeps 11px form labels
legible after the model's own downscale. We also pair every capture with an
accessibility outline: pixels are the better representation for *layout*, the
a11y tree is the better one for *what is answerable*.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Keep each tile comfortably inside the raster ceiling.
MAX_TILE_PX = 4000


@dataclass
class PageCapture:
    tiles: list[Path] = field(default_factory=list)
    aria: str = ""
    page_height: int = 0
    viewport_height: int = 0
    url: str = ""
    title: str = ""

    @property
    def tile_count(self) -> int:
        return len(self.tiles)


async def settle(page: Any, *, quiet_ms: int = 600, timeout_ms: int = 15_000) -> None:
    """Wait for the page to stop moving before photographing it.

    ATS forms hydrate late: React mounts, validation banners appear, custom
    dropdowns render after their data loads. Capturing early hands the model a
    skeleton, and it will confidently describe fields that do not exist yet.
    """
    for state in ("domcontentloaded", "networkidle"):
        try:
            await page.wait_for_load_state(state, timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            pass
    await asyncio.sleep(quiet_ms / 1000)


async def dismiss_overlays(page: Any) -> int:
    """Close cookie banners and consent modals that cover the form."""
    dismissed = 0
    candidates = [
        "button:has-text('Accept all')", "button:has-text('Accept All')",
        "button:has-text('Accept cookies')", "button:has-text('I accept')",
        "button:has-text('Got it')", "button:has-text('Agree')",
        "[aria-label='Accept cookies']", "#onetrust-accept-btn-handler",
        ".osano-cm-accept-all",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=2000)
                dismissed += 1
                await asyncio.sleep(0.3)
        except Exception:  # noqa: BLE001
            continue
    if dismissed:
        log.debug("capture.dismissed_overlays", count=dismissed)
    return dismissed


async def expand_sections(page: Any) -> int:
    """Open collapsed regions so their questions are visible to the camera."""
    expanded = 0
    for sel in ("button[aria-expanded='false']",
                "[role='button'][aria-expanded='false']",
                "details:not([open]) > summary"):
        try:
            els = await page.query_selector_all(sel)
        except Exception:  # noqa: BLE001
            continue
        for el in els[:25]:
            try:
                if await el.is_visible():
                    await el.click(timeout=1500)
                    expanded += 1
                    await asyncio.sleep(0.12)
            except Exception:  # noqa: BLE001
                continue
    if expanded:
        log.debug("capture.expanded", count=expanded)
    return expanded


async def prime(page: Any) -> None:
    """Scroll the whole page once so lazy content renders, then return to top."""
    await page.evaluate(
        """async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const step = Math.max(200, window.innerHeight * 0.85);
            let last = -1;
            for (let y = 0; y < document.body.scrollHeight && y !== last; y += step) {
                last = y;
                window.scrollTo(0, y);
                await sleep(70);
            }
            window.scrollTo(0, 0);
            await sleep(120);
        }"""
    )


async def capture(
    page: Any,
    out_dir: str | Path,
    *,
    prefix: str = "page",
    include_aria: bool = True,
    max_tiles: int = 20,
) -> PageCapture:
    """Scroll-primed, viewport-tiled capture plus an accessibility outline."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    await settle(page)
    await dismiss_overlays(page)
    await expand_sections(page)
    await prime(page)
    await settle(page, quiet_ms=350)

    dims = await page.evaluate(
        """() => ({
            h: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
            vh: window.innerHeight,
            vw: window.innerWidth,
        })"""
    )
    page_h, vp_h = int(dims["h"]), int(dims["vh"])

    cap = PageCapture(
        page_height=page_h, viewport_height=vp_h,
        url=page.url, title=(await page.title()),
    )

    # Short page: one shot is both sufficient and cheaper.
    if page_h <= vp_h * 1.2:
        dest = out_dir / f"{prefix}_0.png"
        await page.screenshot(path=str(dest))
        cap.tiles.append(dest)
    else:
        overlap = int(vp_h * 0.12)      # keep a field from being split at a seam
        step = max(1, vp_h - overlap)
        all_offsets = list(range(0, page_h, step))
        offsets = all_offsets[:max_tiles]
        if len(all_offsets) > max_tiles:
            # Truncating here is silent and dangerous: the verifier is later
            # asked to confirm fields it was never shown, and correctly refuses,
            # which looks like a fill failure. Make it visible instead.
            log.warning("capture.truncated", page_h=page_h,
                        needed=len(all_offsets), captured=max_tiles,
                        covered_px=max_tiles * step)
        for i, y in enumerate(offsets):
            await page.evaluate("y => window.scrollTo(0, y)", y)
            await asyncio.sleep(0.22)
            dest = out_dir / f"{prefix}_{i}.png"
            await page.screenshot(path=str(dest))
            cap.tiles.append(dest)
        await page.evaluate("() => window.scrollTo(0, 0)")

    if include_aria:
        try:
            snap = await page.locator("body").aria_snapshot()
            cap.aria = snap[:60_000]
        except Exception as exc:  # noqa: BLE001
            log.debug("capture.aria_unavailable", error=str(exc)[:120])

    log.info("capture.done", tiles=cap.tile_count, page_h=page_h, aria_chars=len(cap.aria))
    return cap
