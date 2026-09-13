"""End-to-end smoke test: dump -> profile -> discover -> dry-run apply.

This is the test that asks whether the thing works, not whether a function
returns the right shape. It drives the real server over HTTP, calls the real
model, hits real job boards, launches the real browser, and asserts on what
lands on disk afterwards. Every assertion is on an output -- a file, a CSV row,
an HTTP body -- never on source text, so it cannot be satisfied by editing the
code to say the right words.

It is skipped unless JOBBOT_E2E=1, because it needs network, an API key, a GUI
session for the headful browser, and several minutes. Run it deliberately:

    JOBBOT_E2E=1 uv run pytest tests/test_smoke_e2e.py -v -s

Stages share one temp workspace and run in order. A failure early on is a
real failure of that stage, not a fixture problem, and later stages fail with
it -- that is the point.

The dry run never submits. RunConfig.dry_run defaults True and cmd_run only
flips it on --submit, which this file never passes. It also passes --no-project
so nothing reaches GitHub. It does fill a real employer's form in a browser and
stop before the submit button; that is what dry run is for.
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    not os.environ.get("JOBBOT_E2E"),
    reason="end-to-end: needs network, ANTHROPIC_API_KEY, a GUI session; set JOBBOT_E2E=1",
)

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).with_name("fixtures")
BOARD = os.environ.get("JOBBOT_E2E_BOARD", "greenhouse:anthropic")

# The screening answers below are the EXAMPLE profile's, i.e. Jane Doe's. They
# are test fixture data about a fictional person, set here so the run has
# something to preflight against. Nothing in this file ever writes screening
# answers for a real user.
JANE_SCREENING = {
    "work_authorization": True, "requires_sponsorship_now": False,
    "requires_sponsorship_future": False, "criminal_history": False,
    "background_check_consent": True, "veteran_status": "I am not a protected veteran",
    "disability_status": "I do not wish to answer", "age_over_18": True,
    "education_degree": "Bachelor's Degree", "previously_employed_here": False,
    # Also in profile.example.yaml. Greenhouse forms at larger companies ask all
    # three; without them the run halts (correctly) before it fills anything.
    "previously_interviewed_here": False,
    "arbitration_agreement": "Yes",
    "policy_acknowledgement": "Yes",
}


# ---------------------------------------------------------------- harness

class Workspace:
    """One temp data dir + profile path + a live server on a free port."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.data = tmp / "data"
        self.profile = tmp / "config" / "profile.yaml"
        self.data.mkdir(parents=True)
        self.profile.parent.mkdir(parents=True)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.log = tmp / "server.log"

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "jobbot.cli", "--profile", str(self.profile),
             "--csv", str(self.data / "applications.csv"),
             "dashboard", "--no-open", "--port", str(self.port)],
            cwd=ROOT, stdout=open(self.log, "w"), stderr=subprocess.STDOUT,
        )
        for _ in range(60):
            try:
                urllib.request.urlopen(self.base + "/api/status", timeout=1)
                return
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.25)
        raise RuntimeError(f"server did not come up; log:\n{self.log.read_text()[-2000:]}")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=10)

    def get(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def post(self, path: str, body: bytes | dict, timeout: int = 240) -> dict:
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json" if isinstance(body, dict)
                     else "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def cli(self, *args: str, timeout: int = 900) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "jobbot.cli", "--profile", str(self.profile),
             "--csv", str(self.data / "applications.csv"), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )


@pytest.fixture(scope="module")
def ws(tmp_path_factory) -> Workspace:
    w = Workspace(tmp_path_factory.mktemp("e2e"))
    w.start()
    yield w
    w.stop()
    print(f"\n--- server log ({w.log}) tail ---\n{w.log.read_text()[-3000:]}")


# ---------------------------------------------------------------- 1. surface

def test_every_endpoint_answers(ws: Workspace) -> None:
    for path in ("/", "/answers", "/profile", "/edit", "/intake", "/lessons",
                 "/log", "/status", "/api/status", "/api/profile",
                 "/api/applications", "/api/answers"):
        code, body, headers = ws.get(path)
        assert code == 200, (path, code)
        assert body, path
        assert "no-store" in headers.get("Cache-Control", ""), \
            f"{path} must not be cacheable -- a stale page is indistinguishable from a bug"
    for path in ("/nosuch", "/f/../../../etc/passwd", "/f/../config/profile.yaml"):
        assert ws.get(path)[0] == 404, path


def test_resume_text_extraction(ws: Workspace) -> None:
    out = ws.post("/api/resume-text", FIX.joinpath("example_resume.pdf").read_bytes())
    assert out["ok"] and out["chars"] > 500
    assert "840ms" in out["text"], "the number on the resume must survive extraction verbatim"


# ---------------------------------------------------------------- 2. intake

def test_intake_organizes_dump_and_resume_into_cards(ws: Workspace) -> None:
    resume = ws.post("/api/resume-text", FIX.joinpath("example_resume.pdf").read_bytes())
    out = ws.post("/api/intake/organize", {
        "dump": FIX.joinpath("example_dump.txt").read_text(),
        "links": {"linkedin": "linkedin.com/in/janedoe", "github": "github.com/janedoe"},
        "resumes": [{"name": "example_resume.pdf", "label": "general", "text": resume["text"]}],
    }, timeout=300)
    assert out["ok"], out.get("error")
    cards = out["cards"]
    kinds = {c["kind"] for c in cards}
    assert len(cards) >= 8, [c["title"] for c in cards]
    for want in ("identity", "experience", "education", "project", "skills"):
        assert want in kinds, f"no {want} card in {sorted(kinds)}"
    assert "screening" not in kinds, "the model must never propose a screening answer"

    roles = [c for c in cards if c["kind"] == "experience"]
    companies = {c["body"].get("company", "").lower() for c in roles}
    assert "example corp" in companies and "tiny startup" in companies
    assert len(roles) == 2, \
        "dump + resume describe the same two jobs; they must merge, not duplicate"

    # every number in the source must come through untouched
    joined = json.dumps(cards)
    for n in ("840ms", "95ms", "38 services", "3.7"):
        assert n in joined, f"{n} from the source is missing from the proposal"

    ws.tmp.joinpath("cards.json").write_text(json.dumps(out, indent=1))


def test_accept_everything_saves_the_whole_profile(ws: Workspace) -> None:
    cards = json.loads(ws.tmp.joinpath("cards.json").read_text())["cards"]
    out = ws.post("/api/intake/apply", {"patches": [c["patch"] for c in cards]})
    assert out["ok"], out.get("errors")
    assert out["roles"] == 2

    d = yaml.safe_load(ws.profile.read_text())
    ident = d["identity"]
    assert (ident["first_name"], ident["last_name"]) == ("Jane", "Doe")
    assert ident["email"] == "jane.doe@example.com"
    assert ident["location"]["city"] == "San Francisco", \
        "flat city/state from the extractor must land under identity.location"
    assert ident["github"].startswith("https://"), "bare domains must get a scheme"

    roles = {e["company"]: e for e in d["experience"]}
    ex = roles["Example Corp"]
    assert ex["title"] == "Software Engineer"
    assert str(ex["start"]) == "2023-06-01" and ex["end"] is None
    assert any("840ms" in b for b in ex["bullets"])
    assert set(map(str.lower, ex["tech"])) >= {"python", "go"}

    assert d["education"][0]["school"] == "Example University"
    assert d["education"][0]["gpa"] == 3.7
    assert d["education"][0]["completed"] is False

    proj = d["projects"][0]
    assert proj["name"] == "example-project"
    assert proj["url"] == "https://github.com/janedoe/example-project"

    assert sum(len(v) for v in d["skills"].values()) >= 6
    assert {x.lower() for x in d["languages"]} == {"english", "spanish"}
    assert any("icpc" in a.lower() for a in d["awards"])
    assert d["compensation"]["target_base"] == 150000
    assert d["compensation"]["minimum_base"] == 130000
    assert d["notice_period_weeks"] == 2
    assert d["extra"], "the TA work and the frontend preference belong in free-form facts"

    assert d["screening"] == {}, "intake must leave screening untouched"
    assert out["missing_core"], "and the run must still be blocked until the user sets them"
    assert out["missing_identity"] == []


def test_editor_sets_screening_and_preflight_clears(ws: Workspace) -> None:
    """The one step only the user can take, done the way the editor does it."""
    code, body, _ = ws.get("/api/profile")
    assert code == 200
    form = json.loads(body)["profile"]
    # the editor sends skills/extra as rows; from_form also accepts the stored
    # shape, so pass the profile back as-is with screening filled in
    form["screening"] = dict(JANE_SCREENING)
    out = ws.post("/api/profile", form)
    assert out["ok"], out.get("errors")
    assert out["missing_core"] == [], out["missing_core"]
    assert out["missing_identity"] == []
    assert out["screening_set"] == len(JANE_SCREENING)

    d = yaml.safe_load(ws.profile.read_text())
    assert d["experience"] and d["projects"], "setting screening must not clobber the rest"
    assert d["screening"]["criminal_history"]["value"] is False
    assert d["screening"]["criminal_history"]["provenance"] == "confirmed"

    st = json.loads(ws.get("/api/status")[1])
    by = {c["name"]: c for c in st["checks"]}
    assert by["profile validates"]["ok"]
    assert by["core screening answers set"]["ok"]

    chk = ws.cli("check", timeout=120)
    assert "all legally significant answers confirmed" in chk.stdout, chk.stdout + chk.stderr


# ---------------------------------------------------------------- 3. discover

def test_discover_finds_and_ranks_jobs(ws: Workspace) -> None:
    assert ws.profile.exists(), "stages are sequential: this needs the profile stage 4 wrote"
    r = ws.cli("discover", "--source", BOARD, "--limit", "10", timeout=180)
    assert r.returncode == 0, r.stderr
    # a ranked row starts with the fit score as printed by cmd_discover: `{m:5.2f}`
    rows = [l for l in r.stdout.splitlines() if re.match(r"^\s*\d\.\d\d\s", l)]
    assert len(rows) >= 1, r.stdout
    assert "postings from 1 source(s)" in r.stdout
    fits = [float(l.split()[0]) for l in rows]
    assert fits == sorted(fits, reverse=True), "must be ranked best fit first"
    assert fits[0] > 0, "a Software Engineer profile must fit at least one posting on a tech board"


# ---------------------------------------------------------------- 4. apply

def test_dry_run_applies_to_one_posting_and_records_everything(ws: Workspace) -> None:
    r = ws.cli("run", "--source", BOARD, "--limit", "1", "--no-project", timeout=900)
    print(r.stdout[-3000:], r.stderr[-3000:])
    assert r.returncode == 0, r.stderr[-2000:]
    assert "DRY RUN" in r.stdout, "the default must be a dry run and must say so"

    apps = list(csv.DictReader(open(ws.data / "applications.csv")))
    assert apps, "every posting considered must be in the tracker"
    attempted = [a for a in apps if a["status"] not in
                 ("filtered_out", "ghost_suspected", "discovered", "unreachable")]
    assert attempted, f"nothing was attempted; statuses: {[a['status'] for a in apps]}"
    assert not any(a["status"] in ("submitted", "confirmed") for a in apps), \
        "a dry run must never submit"

    a = attempted[0]
    print("attempted:", a["title"], "@", a["company"], "->", a["status"], "|", a["error"])
    audit = Path(a["audit_dir"])
    assert audit.is_absolute() and ws.data in audit.parents, \
        f"artifacts must live beside the tracker, not in ./data of the cwd: {audit}"
    assert audit.is_dir()
    assert (audit / "form.json").exists(), "checkpoint 1 must record the parsed form"
    form = json.loads((audit / "form.json").read_text())
    assert form["fields"], "a real application form has fields"
    shots = list((audit / "screenshots").glob("*.png"))
    assert shots, "every checkpoint captures the page"

    if a["status"] == "prepared":
        assert (audit / "resume.pdf").stat().st_size > 1000
        assert (audit / "answers.json").exists()
        assert (audit / "verification.json").exists()
        answers = json.loads((audit / "answers.json").read_text())
        assert answers

        # Only entries that were actually written count. A null value carrying
        # needs_human is a field deliberately left blank -- the correct outcome
        # for a question the profile cannot answer, not a defect.
        submitted = [x for x in answers if not x.get("needs_human")]
        assert submitted
        assert all(x.get("value") is not None for x in submitted)
        assert "None" not in {str(x["value"]) for x in submitted}, \
            "the literal string None must never be typed into a form"
        for x in answers:
            if x.get("needs_human"):
                assert x.get("value") is None and x.get("blocked_reason"), \
                    f"a blank must say why it is blank: {x.get('label')}"
        emails = [x for x in answers if "mail" in (x.get("label") or "").lower()]
        if emails:
            assert emails[0]["value"] == "jane.doe@example.com"
        legal = [x for x in answers if x.get("source") == "composed"
                 and any(k in (x.get("label") or "").lower()
                         for k in ("authoriz", "sponsor", "convict", "veteran", "disabilit"))]
        assert not legal, f"model-composed answer to a legal question: {legal}"

        rows = list(csv.DictReader(open(ws.data / "answers.csv")))
        assert rows and all(row["job_id"] == a["job_id"] for row in rows)
        assert len(rows) == len(answers), "answers.csv must hold every answer, incl. healed ones"
    elif a["status"] == "needs_human":
        # legitimate outcome: the form asked something the fixture cannot answer
        assert (audit / "needs_human.txt").exists() or a["error"], a
    elif a["status"] == "knockout_fail":
        assert a["knockout_reason"], a
    else:
        # `failed` is not an acceptable end state for "applying works". The one
        # time this branch fired it was an Anthropic overload that the client
        # classified as permanent and never retried.
        err = (audit / "error.txt").read_text()[-1500:] if (audit / "error.txt").exists() else a["error"]
        pytest.fail(f"application ended {a['status']!r}: {err}")
