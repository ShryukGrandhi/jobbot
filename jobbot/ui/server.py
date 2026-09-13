"""The brutalist UI server: the old JSON APIs, plus a live browser and runs.

Serves one page (jobbot/ui/app.html) at `/` and layers new routes over the
existing dashboard handler, so every data endpoint the old views used keeps
working unchanged. The intake wizard and profile editor are mounted in the
shell as same-origin frames; their forms and model calls are untouched.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import structlog

from jobbot.dashboard import Dash, Handler
from jobbot.ui.live import BrowserLive, frame_bytes

log = structlog.get_logger(__name__)
HERE = Path(__file__).resolve().parent


class UIHandler(Handler):
    """New routes first; anything else falls through to the classic dashboard."""

    live: BrowserLive

    def __init__(self, dash: Dash, live: BrowserLive, *a: Any, **kw: Any) -> None:
        self.live = live
        super().__init__(dash, *a, **kw)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/":
                self._send((HERE / "app.html").read_bytes())
            elif path == "/classic":
                self._send(self.dash.overview())
            elif path == "/api/live/status":
                self._json(self.live.status())
            elif path == "/api/live/frame":
                fr = self.live.latest_frame()
                since = int((qs.get("since") or ["0"])[0])
                if fr.seq == since or not fr.data_b64:
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = frame_bytes(fr)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Seq", str(fr.seq))
                self.send_header("X-Frame-Size", f"{fr.width}x{fr.height}")
                self.send_header("X-Page-Url", fr.url.encode("ascii", "replace").decode())
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/queue.json":
                q = self.dash._queue()
                show = (qs.get("show") or ["all"])[0]
                search = (qs.get("q") or [""])[0].strip().lower()
                rows = []
                for e in q.all():
                    if show != "all" and e.decision != show:
                        continue
                    if search and search not in f"{e.company} {e.title}".lower():
                        continue
                    rows.append(e.to_dict())
                self._json({"counts": q.counts(), "rows": rows[:400], "total": len(rows)})
            else:
                super().do_GET()
        except Exception as exc:  # noqa: BLE001
            log.warning("ui.get_failed", path=path, error=repr(exc)[:200])
            try:
                self._json({"ok": False, "error": repr(exc)[:200]}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/api/live/"):
            super().do_POST()
            return
        length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        try:
            if path == "/api/live/start":
                self._json(self.live.start())
            elif path == "/api/live/navigate":
                url = str(payload.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                self._json(self.live.navigate(url))
            elif path == "/api/live/tab":
                self._json(self.live.new_tab(str(payload.get("url") or "about:blank")))
            elif path == "/api/live/show":
                self._json(self.live.show_index(int(payload.get("index", 0))))
            elif path == "/api/live/input":
                self._json(self.live.input(payload))
            elif path == "/api/live/run":
                self._json(self.live.start_run(payload))
            else:
                self._json({"ok": False, "error": "no such endpoint"}, 404)
        except Exception as exc:  # noqa: BLE001
            log.warning("ui.post_failed", path=path, error=repr(exc)[:300])
            self._json({"ok": False, "error": str(exc)[:300]}, 500)


def serve(data_dir: str | Path = "data", profile: str | Path = "config/profile.yaml",
          *, port: int = 8766, open_browser: bool = True, host: str = "127.0.0.1") -> None:
    dash = Dash(Path(data_dir), Path(profile))
    live = BrowserLive(data_dir=Path(data_dir), profile_path=Path(profile))
    while True:
        try:
            httpd = ThreadingHTTPServer((host, port), partial(UIHandler, dash, live))
            break
        except OSError as exc:
            if getattr(exc, "errno", None) not in (48, 98, 10048) or port > port + 20:
                raise
            port += 1
    url = f"http://{host}:{port}/"
    dash.public_url = url
    print(f"jobbot ui  {url}", flush=True)
    print("  this machine only; ctrl-c to stop", flush=True)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
