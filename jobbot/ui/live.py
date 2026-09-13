"""The live browser: one persistent, logged-in Chromium the UI can see.

The dashboard runs jobbot in-process and owns the BrowserSession, so the
tab the candidate watches IS the tab the agent fills. Frames come off CDP's
Page.startScreencast as JPEGs; the page draws the newest one on a canvas and
sends clicks, keys and scrolls back through Input.dispatch*Event. That makes
the embedded pane a real browser the candidate can sign in to -- and because
the profile is persistent, the sign-in survives for every later run.

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

# What the screencast asks Chromium for. 1280 wide keeps a frame ~60-120KB.
FRAME_MAX_W, FRAME_MAX_H, FRAME_QUALITY = 1280, 800, 55
# "Idle" means the canvas polls slower; the run loop wakes it up.
POLL_MS_ACTIVE, POLL_MS_IDLE = 150, 700


@dataclass
class Frame:
    data_b64: str = ""            # jpeg
    width: int = 0                # frame pixel size
    height: int = 0
    css_width: int = 0            # viewport size in CSS px, for input mapping
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
    """Owns the loop, the session, the screencast and the run."""

    def __init__(self, *, data_dir: Path, profile_path: Path) -> None:
        self.data_dir = Path(data_dir)
        self.profile_path = Path(profile_path)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._spin, name="browser-live", daemon=True)
        self.session: Any = None
        self.frame = Frame()
        self.run_state = RunState()
        self._cdp: dict[Any, Any] = {}          # page -> CDPSession
        self._current: Any = None               # page being shown
        self._lock = threading.Lock()
        self._started = threading.Event()
        self.thread.start()

    # -- loop plumbing --------------------------------------------------------

    def _spin(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    def call(self, coro: Any, timeout: float = 60.0) -> Any:
        """Run a coroutine on the browser loop from any thread."""
        self._started.wait(5)
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def submit(self, coro: Any) -> None:
        """Fire-and-forget onto the browser loop."""
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
        ctx.on("page", lambda p: asyncio.ensure_future(self._attach(p)))
        for p in list(ctx.pages):
            await self._attach(p)
        log.info("live.session_started")
        return self.session

    async def _attach(self, page: Any) -> None:
        """Screencast every page; show the newest one."""
        if page in self._cdp or page.is_closed():
            return
        try:
            cdp = await self.session.ctx.new_cdp_session(page)
        except Exception as exc:  # noqa: BLE001
            log.warning("live.cdp_failed", error=str(exc)[:120])
            return
        self._cdp[page] = cdp

        async def ack(params: dict[str, Any]) -> None:
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:  # noqa: BLE001
                return
            if self._current is page:
                meta = params.get("metadata", {})
                with self._lock:
                    self.frame = Frame(
                        data_b64=params["data"],
                        width=int(meta.get("deviceWidth", 0)),
                        height=int(meta.get("deviceHeight", 0)),
                        css_width=int(meta.get("deviceWidth", 0)),
                        css_height=int(meta.get("deviceHeight", 0)),
                        seq=self.frame.seq + 1, url=page.url,
                        title=self._title_cache.get(page, ""), at=time.time())

        cdp.on("Page.screencastFrame", lambda p: asyncio.ensure_future(ack(p)))
        page.on("close", lambda: asyncio.ensure_future(self._detach(page)))
        page.on("load", lambda: asyncio.ensure_future(self._refresh_title(page)))
        await self._show(page)

    _title_cache: dict[Any, str] = {}

    async def _refresh_title(self, page: Any) -> None:
        try:
            self._title_cache[page] = await page.title()
        except Exception:  # noqa: BLE001
            pass

    async def _show(self, page: Any) -> None:
        """Route the screencast to one page."""
        if self._current is page:
            return
        prev = self._current
        if prev is not None and prev in self._cdp and not prev.is_closed():
            try:
                await self._cdp[prev].send("Page.stopScreencast")
            except Exception:  # noqa: BLE001
                pass
        self._current = page
        await self._refresh_title(page)
        try:
            await self._cdp[page].send("Page.startScreencast", {
                "format": "jpeg", "quality": FRAME_QUALITY,
                "maxWidth": FRAME_MAX_W, "maxHeight": FRAME_MAX_H, "everyNthFrame": 1})
        except Exception as exc:  # noqa: BLE001
            log.warning("live.screencast_failed", error=str(exc)[:120])

    async def _detach(self, page: Any) -> None:
        self._cdp.pop(page, None)
        self._title_cache.pop(page, None)
        if self._current is page:
            self._current = None
            # fall back to the newest live page, if any
            for p in reversed(list(self.session.ctx.pages)):
                if p in self._cdp and not p.is_closed():
                    await self._show(p)
                    break

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
                await self._attach(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await self._refresh_title(page)
            return {"ok": True, "url": page.url}
        return self.call(go(), timeout=90)

    def new_tab(self, url: str = "about:blank") -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            await self._ensure_session()
            page = await self.session.ctx.new_page()
            await self._attach(page)
            await self._show(page)
            if url and url != "about:blank":
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            return {"ok": True}
        return self.call(go(), timeout=90)

    def show_index(self, i: int) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            pages = [p for p in self.session.ctx.pages if not p.is_closed()]
            if 0 <= i < len(pages):
                await self._attach(pages[i])
                await self._show(pages[i])
            return {"ok": True}
        return self.call(go(), timeout=30)

    def input(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Forward a pointer or keyboard event from the canvas to the page.

        Coordinates arrive as fractions of the frame (0..1) so the browser
        window size never has to match the canvas.
        """
        async def go() -> dict[str, Any]:
            page = self._current
            if page is None or page.is_closed() or page not in self._cdp:
                return {"ok": False, "error": "no page"}
            cdp = self._cdp[page]
            fr = self.latest_frame()
            x = float(ev.get("fx", 0)) * (fr.css_width or 1)
            y = float(ev.get("fy", 0)) * (fr.css_height or 1)
            t = ev.get("type")
            if t in ("click", "dblclick"):
                n = 2 if t == "dblclick" else 1
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": x, "y": y})
                for _ in range(n):
                    await cdp.send("Input.dispatchMouseEvent", {
                        "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": n})
                    await cdp.send("Input.dispatchMouseEvent", {
                        "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": n})
            elif t == "move":
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            elif t == "wheel":
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": x, "y": y,
                    "deltaX": float(ev.get("dx", 0)), "deltaY": float(ev.get("dy", 0))})
            elif t == "text":
                await cdp.send("Input.insertText", {"text": str(ev.get("text", ""))})
            elif t == "key":
                key = str(ev.get("key", ""))
                code = str(ev.get("code", ""))
                mods = int(ev.get("modifiers", 0))
                base = {"key": key, "code": code, "modifiers": mods}
                win = _WINDOWS_VK.get(key)
                if win is not None:
                    base["windowsVirtualKeyCode"] = win
                    base["nativeVirtualKeyCode"] = win
                await cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **base})
                await cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
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


# Keys the page sends by name; Chromium wants virtual key codes for these or
# the keydown does nothing in inputs.
_WINDOWS_VK = {
    "Enter": 13, "Tab": 9, "Backspace": 8, "Delete": 46, "Escape": 27,
    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34, " ": 32,
}


def frame_bytes(fr: Frame) -> bytes:
    return base64.b64decode(fr.data_b64) if fr.data_b64 else b""
