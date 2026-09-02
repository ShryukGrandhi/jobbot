"""Stealth browser session with a hard tab budget.

CloakBrowser ships a Chromium whose fingerprints are patched at the C++ source
level and exposes the Playwright API. Its free tier permits one concurrent
session, which suits this design: a single persistent context (so per-tenant
Workday and Greenhouse logins survive between runs) with a capped number of
tabs inside it.

One persistent profile, not one per ATS. Cookies are already origin-scoped, so
splitting buys no isolation -- and a profile that accumulates real history,
cache and cookies over weeks scores better against device fingerprinting than a
fresh one each run. Do not delete this directory; back it up.

Tab discipline is a correctness property, not tidiness. Each open tab holds a
half-finished application. If the process dies with eight of them open we cannot
tell which were submitted, and a duplicate submission to a real employer is not
an error we can retract.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import structlog
from cloakbrowser import launch_persistent_context_async

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = REPO_ROOT / ".profiles" / "main"
MAX_TABS = int(os.environ.get("JOBBOT_MAX_TABS", "3"))


@dataclass
class BrowserConfig:
    profile_dir: Path = DEFAULT_PROFILE
    # Headful is required: it renders the real application and Gmail UIs, and a
    # headless shell is materially easier to fingerprint than a real window.
    headless: bool = False
    humanize: bool = True          # bezier mouse, per-character typing, real scroll
    human_preset: str = "default"
    max_tabs: int = MAX_TABS
    # No proxy by default. A real residential IP with history is the single best
    # asset in this stack; a rented proxy exit is shared, listed, and a downgrade.
    proxy: str | None = None
    geoip: bool = False
    locale: str = "en-US"
    timezone: str | None = None
    nav_timeout_ms: int = 60_000
    launch_timeout_s: float = 120.0
    downloads_dir: Path | None = None
    extra_launch_kwargs: dict[str, Any] = field(default_factory=dict)


class BrowserSession:
    """Owns the single persistent context and rations tabs across coroutines."""

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig()
        self.ctx: Any = None
        self._sem = asyncio.Semaphore(self.config.max_tabs)
        self._live: set[Any] = set()          # tabs currently leased out
        self._launched_at: float | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self.ctx is not None:
                return
            self.config.profile_dir.mkdir(parents=True, exist_ok=True)

            kwargs: dict[str, Any] = dict(
                headless=self.config.headless,
                humanize=self.config.humanize,
                human_preset=self.config.human_preset,
                locale=self.config.locale,
                accept_downloads=True,
                **self.config.extra_launch_kwargs,
            )
            if self.config.proxy:
                kwargs["proxy"] = self.config.proxy
                kwargs["geoip"] = self.config.geoip
            if self.config.timezone:
                kwargs["timezone"] = self.config.timezone
            if self.config.downloads_dir:
                self.config.downloads_dir.mkdir(parents=True, exist_ok=True)
                kwargs["downloads_path"] = str(self.config.downloads_dir)

            try:
                self.ctx = await asyncio.wait_for(
                    launch_persistent_context_async(str(self.config.profile_dir), **kwargs),
                    timeout=self.config.launch_timeout_s,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"browser launch exceeded {self.config.launch_timeout_s}s -- "
                    "a stale Chromium may still hold this profile directory"
                ) from exc

            self.ctx.set_default_navigation_timeout(self.config.nav_timeout_ms)
            self.ctx.set_default_timeout(self.config.nav_timeout_ms)
            self._launched_at = time.time()

            # A persistent context launches with a blank page attached. Close it
            # rather than pooling it: leasing one shared page to concurrent
            # callers is a race, and keeping it as a spare silently makes the
            # real ceiling max_tabs + 1.
            for p in list(self.ctx.pages):
                if p.url in ("about:blank", ""):
                    with contextlib.suppress(Exception):
                        await p.close()

            log.info(
                "browser.start",
                profile=str(self.config.profile_dir),
                headless=self.config.headless,
                max_tabs=self.config.max_tabs,
            )

    async def close(self) -> None:
        if self.ctx is None:
            return
        with contextlib.suppress(Exception):
            await self.ctx.close()
        log.info("browser.close", uptime_s=round(time.time() - (self._launched_at or 0), 1))
        self.ctx = None
        self._live.clear()

    @property
    def live_tabs(self) -> int:
        return len(self._live)

    @property
    def total_pages(self) -> int:
        return len(self.ctx.pages) if self.ctx is not None else 0

    @contextlib.asynccontextmanager
    async def tab(self, label: str = "tab") -> AsyncIterator[Any]:
        """Lease one tab from the budget. Always closed on exit.

        Blocks rather than raising when the budget is full, so callers
        self-throttle instead of needing their own queue.
        """
        if self.ctx is None:
            await self.start()
        await self._sem.acquire()
        page = None
        try:
            page = await self.ctx.new_page()
            self._live.add(page)
            log.debug("browser.tab_open", label=label, live=len(self._live))
            yield page
        finally:
            if page is not None:
                self._live.discard(page)
                with contextlib.suppress(Exception):
                    if not page.is_closed():
                        await page.close()
            self._sem.release()
            log.debug("browser.tab_closed", label=label, live=len(self._live))

    async def reap_orphans(self) -> int:
        """Close tabs the governor never issued.

        Application flows open pages we did not ask for: OAuth popups, PDF
        previews, "apply on the company site" redirects. Unreaped they breach
        the budget we promised and leak memory across a long run.
        """
        if self.ctx is None:
            return 0
        killed = 0
        for p in list(self.ctx.pages):
            if p not in self._live and not p.is_closed():
                with contextlib.suppress(Exception):
                    await p.close()
                    killed += 1
        if killed:
            log.info("browser.reaped_orphans", count=killed)
        return killed

    @contextlib.asynccontextmanager
    async def expect_popup(self, page: Any) -> AsyncIterator[Any]:
        """Capture a popup the site opens so it counts against the budget.

        Workday in particular launches the application from the posting in a new
        window; without owning that handle we cannot close it.
        """
        async with page.context.expect_page() as info:
            yield info
        popup = await info.value
        self._live.add(popup)
