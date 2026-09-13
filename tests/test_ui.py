"""The brutalist UI server: routes answer, the frame endpoint is cheap, and
nothing here needs a browser -- BrowserLive is faked."""

from __future__ import annotations

import json
import threading
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jobbot.dashboard import Dash
from jobbot.ui.live import Frame
from jobbot.ui.server import UIHandler


class FakeLive:
    def __init__(self) -> None:
        self.frame = Frame()
        self.inputs: list[dict] = []
        self.runs: list[dict] = []

    def status(self):
        return {"browser": "down", "pages": [], "frame_seq": self.frame.seq,
                "run": {"running": False}, "log": [], "poll_ms": 700}

    def latest_frame(self):
        return self.frame

    def start(self):
        return self.status()

    def navigate(self, url):
        return {"ok": True, "url": url}

    def new_tab(self, url="about:blank"):
        return {"ok": True}

    def show_index(self, i):
        return {"ok": True}

    def input(self, ev):
        self.inputs.append(ev); return {"ok": True}

    def start_run(self, spec):
        self.runs.append(spec); return {"ok": True}


@pytest.fixture
def server(tmp_path: Path):
    data = tmp_path / "data"; data.mkdir()
    (data / "applications.csv").write_text(
        "job_id,company,title,ats,status,match_score\ngh:1,Acme,SWE,greenhouse,prepared,0.9\n",
        encoding="utf-8")
    (data / "answers.csv").write_text(
        "recorded_at,job_id,company,title,ats,job_url,field_label,field_kind,required,answer,"
        "source,confidence,rationale,left_blank,blank_reason\n", encoding="utf-8")
    (data / "queue.json").write_text(json.dumps({
        "gh:1": {"job_id": "gh:1", "company": "Acme", "title": "SWE", "url": "https://x",
                 "decision": "pending", "fit": 0.9, "posted": "2026-09-01"},
        "gh:2": {"job_id": "gh:2", "company": "Zed", "title": "Intern", "url": "https://y",
                 "decision": "approved", "fit": 0.7, "posted": ""}}), encoding="utf-8")
    live = FakeLive()
    dash = Dash(data, tmp_path / "profile.yaml")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(UIHandler, dash, live))
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, live
    httpd.shutdown(); httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read(), dict(r.headers)


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


def test_shell_and_data_routes_answer(server):
    base, _ = server
    code, body, h = _get(base + "/")
    assert code == 200 and b"Live browser" in body and b"start run" in body
    assert "no-store" in h.get("Cache-Control", "")
    code, body, _ = _get(base + "/api/queue.json?show=approved")
    q = json.loads(body)
    assert q["counts"]["approved"] == 1 and [r["job_id"] for r in q["rows"]] == ["gh:2"]
    code, body, _ = _get(base + "/api/queue.json?q=acme")
    assert [r["company"] for r in json.loads(body)["rows"]] == ["Acme"]
    code, body, _ = _get(base + "/api/live/status")
    assert json.loads(body)["browser"] == "down"
    code, body, _ = _get(base + "/classic")
    assert code == 200 and b"jobbot" in body, "the old dashboard is still reachable"


def test_frame_endpoint_is_204_until_a_new_frame_exists(server):
    base, live = server
    req = urllib.request.Request(base + "/api/live/frame?since=0")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 204
    live.frame = Frame(data_b64="/9j/4AAQ", width=10, height=5, seq=3, url="https://x")
    code, body, h = _get(base + "/api/live/frame?since=0")
    assert code == 200 and h["Content-Type"] == "image/jpeg" and h["X-Frame-Seq"] == "3"
    assert body.startswith(b"\xff\xd8")
    with urllib.request.urlopen(base + "/api/live/frame?since=3", timeout=10) as r:
        assert r.status == 204, "the same frame is never re-sent"


def test_input_and_run_are_forwarded(server):
    base, live = server
    _post(base + "/api/live/input", {"type": "click", "fx": 0.5, "fy": 0.25})
    assert live.inputs == [{"type": "click", "fx": 0.5, "fy": 0.25}]
    _post(base + "/api/live/run", {"sources": ["greenhouse:acme"], "limit": 2, "submit": False})
    assert live.runs[0]["sources"] == ["greenhouse:acme"] and live.runs[0]["submit"] is False
    code, out = _post(base + "/api/live/navigate", {"url": "example.com"})
    assert out["url"] == "https://example.com", "a bare host gets a scheme"
