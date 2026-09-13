"""The live browser: one persistent, logged-in Chromium the UI can see.

The dashboard runs jobbot in-process and owns the BrowserSession, so the tab
the candidate watches IS the tab the agent fills. Everything here goes through
Playwright's own connection -- frames are `page.screenshot` polls, input is
`page.mouse`/`page.keyboard`. That is deliberate: an earlier version opened a
second CDP session per page for `Page.startScreencast`, and two CDP consumers
on one target crashed the page the moment the agent's own capture took a
screenshot. One connection, no collision.

Everything Playwright touches lives on one asyncio loop in one background
thread. HTTP handlers hand coroutines to that loop and wait for the result.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# JPEG quality for the streamed frame. Low: it is a preview, not a recording.
FRAME_QUALITY = 45
# How often to grab a frame. Faster during a run so filling looks live; slower
# when idle. A grab is skipped while the agent is mid-screenshot (see _poll).
POLL_MS_ACTIVE, POLL_MS_IDLE = 500, 1000


@dataclass
class Frame:
    data_b64: str = ""            # jpeg, base64
    width: int = 0                # css px (frame is 1:1 with the viewport)
    height: int = 0
    css_width: int = 0
    css_height: int = 0
    seq: int = 0
    url: str = ""
    title: str = ""
    at: float = 0.0


@dataclass
class RunState:
    running: bool = False
    label: str = ""
    started_at: float = 0.0
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    log: list[str] = field(default_factory=list)   # last N lines for the UI

    def say(self, line: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {line}")
        del self.log[:-200]


class BrowserLive:
    """Owns the loop, the session, the frame poller and the run."""

    def __init__(self, *, data_dir: Path, profile_path: Path) -> None:
        self.data_dir = Path(data_dir)
        self.profile_path = Path(profile_path)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._spin, name="browser-live", daemon=True)
        self.session: Any = None
        self.frame = Frame()
        self.run_state = RunState()
        self._current: Any = None               # page being shown
        self._title_cache: dict[Any, str] = {}
        self._lock = threading.Lock()
        self._started = threading.Event()
        self._poller: Any = None
        # Serializes screenshot grabs against the agent's own page ops. Held
        # around every Playwright call the poller makes so a frame grab never
        # races the orchestrator on the same page.
        self._io = asyncio.Lock()
        self.thread.start()

    # -- loop plumbing --------------------------------------------------------

    def _spin(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    def call(self, coro: Any, timeout: float = 60.0) -> Any:
        self._started.wait(5)
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def submit(self, coro: Any) -> None:
        self._started.wait(5)
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    # -- browser --------------------------------------------------------------

    async def _ensure_session(self) -> Any:
        if self.session is not None and self.session.ctx is not None:
            return self.session
        from jobbot.browser.session import BrowserConfig, BrowserSession

        self.session = BrowserSession(BrowserConfig(headless=False))
        await self.session.start()
        ctx = self.session.ctx
        ctx.on("page", lambda p: self._track(p))
        for p in list(ctx.pages):
            self._track(p)
        # Show the newest live page.
        live = [p for p in ctx.pages if not p.is_closed()]
        self._current = live[-1] if live else None
        if self._poller is None:
            self._poller = asyncio.ensure_future(self._poll())
        log.info("live.session_started")
        return self.session

    def _track(self, page: Any) -> None:
        """Watch a page's lifecycle; show it when it appears, follow closes."""
        if page.is_closed():
            return
        page.on("close", lambda *_: self._on_close(page))
        page.on("load", lambda *_: asyncio.ensure_future(self._refresh_title(page)))
        # A freshly opened page becomes the shown one -- this is how the pane
        # follows the tab the agent opens for an application.
        self._current = page
        asyncio.ensure_future(self._refresh_title(page))

    def _on_close(self, page: Any) -> None:
        self._title_cache.pop(page, None)
        if self._current is page:
            self._current = None
            if self.session and self.session.ctx:
                for p in reversed(list(self.session.ctx.pages)):
                    if not p.is_closed():
                        self._current = p
                        break

    async def _refresh_title(self, page: Any) -> None:
        try:
            async with self._io:
                self._title_cache[page] = await page.title()
        except Exception:  # noqa: BLE001
            pass

    async def _poll(self) -> None:
        """Grab a JPEG of the shown page on a timer, forever."""
        while True:
            page = self._current
            interval = POLL_MS_ACTIVE if self.run_state.running else POLL_MS_IDLE
            if page is not None and not page.is_closed():
                got = await self._grab(page)
                if not got:
                    interval = 250    # transient; try again soon
            await asyncio.sleep(interval / 1000)

    async def _grab(self, page: Any) -> bool:
        # Non-blocking on the lock: if the agent is mid-operation, skip this
        # frame rather than queue behind a long capture and stall the pane.
        if self._io.locked():
            return False
        try:
            async with self._io:
                data = await page.screenshot(type="jpeg", quality=FRAME_QUALITY, timeout=4000)
                size = await page.evaluate("() => [innerWidth, innerHeight]")
        except Exception:  # noqa: BLE001
            return False
        w, h = int(size[0]), int(size[1])
        with self._lock:
            self.frame = Frame(
                data_b64=base64.b64encode(data).decode(), width=w, height=h,
                css_width=w, css_height=h, seq=self.frame.seq + 1,
                url=page.url, title=self._title_cache.get(page, ""), at=time.time())
        return True

    async def _show(self, page: Any) -> None:
        if not page.is_closed():
            self._current = page
            await self._refresh_title(page)

    # -- public: called from HTTP handlers (any thread) --------------------------

    def start(self) -> dict[str, Any]:
        self.call(self._ensure_session(), timeout=180)
        return self.status()

    def status(self) -> dict[str, Any]:
        s = self.session
        pages = []
        if s is not None and s.ctx is not None:
            for p in s.ctx.pages:
                if p.is_closed():
                    continue
                pages.append({"url": p.url, "title": self._title_cache.get(p, ""),
                              "current": p is self._current})
        return {
            "browser": "up" if s is not None and s.ctx is not None else "down",
            "pages": pages,
            "frame_seq": self.frame.seq,
            "run": {k: v for k, v in vars(self.run_state).items() if k != "log"},
            "log": self.run_state.log[-60:],
            "poll_ms": POLL_MS_ACTIVE if self.run_state.running else POLL_MS_IDLE,
        }

    def latest_frame(self) -> Frame:
        with self._lock:
            return self.frame

    def navigate(self, url: str) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            await self._ensure_session()
            page = self._current
            if page is None or page.is_closed():
                page = await self.session.ctx.new_page()
            async with self._io:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await self._show(page)
            return {"ok": True, "url": page.url}
        return self.call(go(), timeout=90)

    def new_tab(self, url: str = "about:blank") -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            await self._ensure_session()
            page = await self.session.ctx.new_page()
            await self._show(page)
            if url and url != "about:blank":
                async with self._io:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return {"ok": True}
        return self.call(go(), timeout=90)

    def show_index(self, i: int) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            pages = [p for p in self.session.ctx.pages if not p.is_closed()]
            if 0 <= i < len(pages):
                await self._show(pages[i])
            return {"ok": True}
        return self.call(go(), timeout=30)

    def input(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Forward a pointer or keyboard event to the shown page.

        Coordinates arrive as fractions of the frame (0..1) so the pane size
        never has to match the browser window. All through Playwright's mouse
        and keyboard -- no CDP.
        """
        async def go() -> dict[str, Any]:
            page = self._current
            if page is None or page.is_closed():
                return {"ok": False, "error": "no page"}
            fr = self.latest_frame()
            x = float(ev.get("fx", 0)) * (fr.css_width or 1)
            y = float(ev.get("fy", 0)) * (fr.css_height or 1)
            t = ev.get("type")
            try:
                async with self._io:
                    if t == "click":
                        await page.mouse.click(x, y)
                    elif t == "dblclick":
                        await page.mouse.dblclick(x, y)
                    elif t == "move":
                        await page.mouse.move(x, y)
                    elif t == "wheel":
                        await page.mouse.move(x, y)
                        await page.mouse.wheel(float(ev.get("dx", 0)), float(ev.get("dy", 0)))
                    elif t == "text":
                        await page.keyboard.insert_text(str(ev.get("text", "")))
                    elif t == "key":
                        await page.keyboard.press(_pw_key(str(ev.get("key", ""))))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)[:120]}
            return {"ok": True}
        return self.call(go(), timeout=15)

    # -- the run ---------------------------------------------------------------

    def start_run(self, spec: dict[str, Any]) -> dict[str, Any]:
        if self.run_state.running:
            return {"ok": False, "error": "a run is already in progress"}
        self.run_state = RunState(running=True, label=", ".join(spec.get("sources") or []),
                                  started_at=time.time())
        self.submit(self._run(spec))
        return {"ok": True}

    async def _run(self, spec: dict[str, Any]) -> None:
        st = self.run_state
        try:
            from dotenv import load_dotenv
            load_dotenv()
            from jobbot.cli import _collect, _standard_resume
            from jobbot.llm.client import LLMClient
            from jobbot.orchestrator import HaltWithTabOpen, Orchestrator, RunConfig
            from jobbot.profile import Profile
            from jobbot.queue import JobQueue
            from jobbot.tracker.csv_tracker import Status, Tracker

            sources = [s for s in (spec.get("sources") or []) if s.strip()]
            limit = int(spec.get("limit") or 1)
            submit = bool(spec.get("submit"))
            approved_only = bool(spec.get("approved", True))

            st.say(f"discovering: {', '.join(sources)}")
            posts = await _collect(sources, 200)
            profile = Profile.load(self.profile_path)
            tracker = Tracker(self.data_dir / "applications.csv")
            queue = JobQueue(self.data_dir / "queue.json")
            queue.add_posts(posts, {})
            queue.save()
            if approved_only:
                ok = queue.approved_ids()
                posts = [p for p in posts if p.job_id in ok]
                st.say(f"{len(posts)} approved job(s) in these sources")
                if not posts:
                    st.say("nothing approved -- tick jobs in the queue first")
                    return
            session = await self._ensure_session()
            llm = LLMClient()

            class _A:  # what _standard_resume expects
                resume = spec.get("resume") or None
                tailor = bool(spec.get("tailor"))
            standard = _standard_resume(_A, self.data_dir)

            cfg = RunConfig(
                data_dir=self.data_dir, dry_run=not submit, standard_resume=standard,
                make_github_project=bool(spec.get("project", False)),
                per_company_cap=10_000 if approved_only else RunConfig.per_company_cap,
            )
            st.say(f"{'SUBMIT' if submit else 'dry run'}: {len(posts)} posting(s), limit {limit}")
            orch = Orchestrator(profile, session, llm, tracker, cfg)
            try:
                results = await orch.run(posts, limit=limit)
            except HaltWithTabOpen as halt:
                st.say(f"HALTED with the tab open on {halt.job_id}: "
                       + "; ".join(b.label[:60] for b in halt.blockers))
                results = []
            for r in results:
                st.results.append({"job_id": r.job_id, "status": r.status, "reason": r.reason[:200]})
                st.say(f"{r.job_id}  {r.status}  {r.reason[:80]}")
                if r.status in (Status.SUBMITTED.value, Status.CONFIRMED.value):
                    queue.decide(r.job_id, "applied")
            queue.save()
            kept = session.kept_tabs
            if kept:
                st.say(f"{len(kept)} tab(s) left open for you: " + "; ".join(kept))
            st.say("run finished")
        except Exception as exc:  # noqa: BLE001
            st.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            st.say("run failed: " + st.error)
            log.error("live.run_failed", error=st.error)
        finally:
            st.running = False


# Page.keyboard.press names: our JS sends DOM key values; most match Playwright
# key names, but a few need translating.
_PW_KEYS = {
    " ": "Space", "Enter": "Enter", "Tab": "Tab", "Backspace": "Backspace",
    "Delete": "Delete", "Escape": "Escape", "ArrowLeft": "ArrowLeft",
    "ArrowUp": "ArrowUp", "ArrowRight": "ArrowRight", "ArrowDown": "ArrowDown",
    "Home": "Home", "End": "End", "PageUp": "PageUp", "PageDown": "PageDown",
}


def _pw_key(key: str) -> str:
    return _PW_KEYS.get(key, key)


def frame_bytes(fr: Frame) -> bytes:
    return base64.b64decode(fr.data_b64) if fr.data_b64 else b""
