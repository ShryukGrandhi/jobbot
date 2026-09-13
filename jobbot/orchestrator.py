"""End-to-end pipeline for one application, and the loop over many.

Stage order is the single most consequential design decision here, and it is
driven by cost. The expensive, irreversible work -- generating a GitHub project,
tailoring and rendering a resume -- happens only AFTER we have confirmed the
form is reachable and winnable.

The failure mode that ordering avoids is documented: one published run generated
2,019 tailored resume files and 1,083 cover letters to produce 112 submissions,
because it tailored before discovering the form needed an account it could not
create. Almost all of that work was thrown away.

So the pipeline is cheap-to-expensive:

    discover -> dedup -> ghost filter -> fit filter        (free, no browser)
    open tab -> detect ATS -> account wall                 (cheap)
    checkpoint 1: parse the form                           (one vision call)
    knockout pre-scan                                      (one cheap call)
    -- only now --
    build GitHub project, tailor resume, render PDF        (expensive)
    fill -> checkpoint 2 + heal loop -> submit             (careful)
    checkpoint 3: did it actually go through?              (one vision call)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from jobbot.ats import credentials as vault
from jobbot.ats import oracle as orc
from jobbot.ats import workday as wd
from jobbot.ats.detect import ATS, REQUIRES_ACCOUNT, detect
from jobbot.browser import capture as cap
from jobbot.browser.session import BrowserSession
from jobbot.discovery.sources import JobPost, ghost_score
from jobbot.forms.fill import apply_answer, q
from jobbot.forms.model import (
    AnswerSource, FieldKind, FormField, ParsedForm, ProposedAnswer,
)
from jobbot.healer import checkpoints as ck
from jobbot.healer.answer import deterministic_answers, model_answers
from jobbot.llm.client import LLMClient
from jobbot.notify import Notification, reliability_score
from jobbot.notify import send as notify_send
from jobbot.profile import LEGALLY_SIGNIFICANT, Profile
from jobbot.resume.render import render_one_page
from jobbot.resume.tailor import fabrication_check, refine, sanitize_skills, tailor
from jobbot.tracker.answers_csv import AnswerLog
from jobbot.tracker.csv_tracker import Application, Status, Tracker

log = structlog.get_logger(__name__)


@dataclass
class RunConfig:
    data_dir: Path = Path("data")
    dry_run: bool = True              # fill everything, stop before submit
    # When set, this exact PDF is uploaded to every application and no resume
    # is generated. The candidate's own document, unchanged.
    standard_resume: Path | None = None
    make_github_project: bool = True
    publish_project_private: bool = False
    max_heal_rounds: int = 4
    # How many times to work the same application before moving on. 0 means
    # keep going until the result is settled (submitted, knocked out, needs
    # the candidate) or MAX_ATTEMPTS is hit. Retries happen inside the same
    # tab, with backoff, so a crash does not throw the tab away.
    attempts_per_job: int = 0
    # Stay on one application until it is submitted. A form that has been
    # filled and healed represents real work and a real tab; abandoning it to
    # start the next job throws that away and leaves the candidate with
    # nothing. With this set, an application that cannot be healed halts the
    # run with its tab still open, to be diagnosed rather than repeated.
    persist_until_submitted: bool = False
    max_resume_rounds: int = 5
    min_match_score: float = 0.5
    max_ghost_score: float = 0.6
    per_company_cap: int = 3
    gmail_enabled: bool = True
    notify: bool = True
    notify_to: str | None = None
    pace_seconds: tuple[float, float] = (25.0, 70.0)


# Buttons that hand the application to someone else's account system.
_THIRD_PARTY_APPLY_JS = """
() => [...document.querySelectorAll('button, a')]
    .map(e => (e.innerText || '').trim())
    .filter(t => /apply with|continue with|sign in with/i.test(t))
    .slice(0, 3)
"""


# The page telling us why it refused, rather than telling us nothing.
_REJECTED_AT_SUBMIT = re.compile(
    r"needs? correction|missing entry|required field|please complete"
    r"|please correct|please check this box|fix the following|is required",
    re.I)


def _looks_rejected(evidence: str, errors: list[str]) -> bool:
    blob = " ".join([evidence or "", *(errors or [])])
    return bool(_REJECTED_AT_SUBMIT.search(blob))


def _worth_retrying(r: "ApplicationResult") -> bool:
    """Is another attempt at this job likely to get further?

    Not for anything settled: a knockout answer will not change, a posting with
    no form will not grow one, and a question only the candidate can answer
    will not answer itself. Everything else -- an unhealed field, a browser
    timeout, a sign-in that has since become possible -- is worth another go.
    """
    if r.status in (Status.SUBMITTED.value, Status.CONFIRMED.value,
                    Status.KNOCKOUT_FAIL.value, "dry_run", "skipped"):
        return False
    settled = ("third-party apply", "no answerable fields",
               "legally significant", "awaiting candidate", "already applied",
               # Retrying a rate limit is what caused it. Oracle answered the
               # third attempt on one posting with "Too Many Attempts. Try
               # Again Later." and locked the rest of the tenant out with it.
               "too many attempts", "try again later", "rate limit")
    blob = f"{r.reason} {' '.join(r.flagged or [])}".lower()
    return not any(s in blob for s in settled)


# Hard ceiling for attempts_per_job=0. Twenty-five is a few hours of one
# posting at the longest backoff; past that the problem is not transient.
MAX_ATTEMPTS = 25
# A form that fills but will not verify clean gets this many full passes
# (navigate, fill, heal) before its tab is left open for the candidate. Each
# pass is minutes of model calls; past three the fix is not going to appear.
MAX_UNCLEAN_PASSES = 3
# Backoff between in-tab retries: 5s, 10s, 20s, ... capped at two minutes.
RETRY_BASE_S, RETRY_CAP_S = 5.0, 120.0

_SETTLED_OK = (Status.SUBMITTED.value, Status.CONFIRMED.value, "dry_run", "skipped")


def _attempt_budget(cfg: "RunConfig") -> int:
    return MAX_ATTEMPTS if cfg.attempts_per_job <= 0 else cfg.attempts_per_job


def _retry_delay(attempt: int) -> float:
    import random
    return min(RETRY_CAP_S, RETRY_BASE_S * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)


# Labels that belong to a sign-in or registration form rather than to an
# application: if these are all a page offers, the gate really is up.
_AUTH_FIELD = re.compile(
    r"password|sign in|log in|email address\b.*(sign|log)|create account", re.I)


# Workday wizards run to five steps; the cap stops a loop on a page that
# keeps saying it advanced.
MAX_WIZARD_STEPS = 8
# How long to give a submit to answer. Ashby's reCAPTCHA alone budgets 30s.
SUBMIT_WAIT_S = 30
# Clicking submit gets retries: the page is often still settling.
SUBMIT_CLICK_TRIES = 5


async def _settle_after_submit(page: Any) -> str:
    """Wait for the page to answer the submit, not for a fixed 2.5 seconds.

    Ashby posts the application, runs an invisible reCAPTCHA whose own budget
    is thirty seconds, and only then re-renders. Screenshotting after a flat
    pause caught the unchanged form, and the outcome pass -- correctly, for
    what it was shown -- reported the application as not submitted.

    Returns what ended the wait, for the log.
    """
    gone = """() => {
      const t = document.body.innerText;
      if (/thank you|application (was )?(received|submitted)|we.{0,3}ve received/i.test(t))
        return 'confirmation text';
      const f = [...document.querySelectorAll('button')].find(b => /submit/i.test(b.innerText));
      return f ? '' : 'submit button gone';
    }"""
    before = page.url
    for _ in range(SUBMIT_WAIT_S):
        await asyncio.sleep(1)
        if page.url != before:
            return "url changed"
        with contextlib.suppress(Exception):
            why = await page.evaluate(gone)
            if why:
                return why
    return "timed out"


class HaltWithTabOpen(RuntimeError):
    """Stop the run without closing the browser, so the form can be inspected."""

    def __init__(self, job_id: str, blockers: list[Any]) -> None:
        self.job_id = job_id
        self.blockers = blockers
        super().__init__(f"{job_id}: {len(blockers)} blocker(s), tab left open")


@dataclass
class ApplicationResult:
    job_id: str
    status: str
    reason: str = ""
    resume_path: str = ""
    project_url: str = ""
    submitted: bool = False
    evidence: str = ""
    flagged: list[str] = field(default_factory=list)
    heal_rounds: int = 0
    lessons: list[dict[str, str]] = field(default_factory=list)


# Role tiers, lowest number = applied to first. Ordering, not exclusion.
# Senior and staff postings sort last because a "6+ years" line is a knockout
# for an early-career candidate, not a stretch goal.
TIER_INTERN, TIER_NEWGRAD, TIER_FULLTIME, TIER_SENIOR = 0, 1, 2, 3

_INTERN_PAT = re.compile(
    r"\bintern(ship)?\b|\bco-?op\b|\bfellow(ship)?\b|\bresiden(t|cy)\b|"
    r"\bapprentice(ship)?\b|\bsummer\b|\bwinter\b|\bstudent\b|\btrainee\b", re.I)
_NEWGRAD_PAT = re.compile(
    r"\bnew ?grad(uate)?\b|\bentry[- ]level\b|\buniversity\b|\bcampus\b|"
    r"\bearly career\b|\bjunior\b|\bassociate\b|\bgrad(uate)? (program|role)\b|\bI\b$", re.I)
_SENIOR_PAT = re.compile(
    r"\bsenior\b|\bsr\.?\b|\bstaff\b|\bprincipal\b|\bdistinguished\b|\blead\b|"
    r"\bmanager\b|\bdirector\b|\bhead of\b|\bvp\b|\barchitect\b", re.I)


def role_tier(post: JobPost) -> int:
    """Which bucket a posting falls in. Drives ordering, not exclusion."""
    t = post.title or ""
    if _INTERN_PAT.search(t):
        return TIER_INTERN
    if _NEWGRAD_PAT.search(t):
        return TIER_NEWGRAD
    if _SENIOR_PAT.search(t):
        return TIER_SENIOR
    return TIER_FULLTIME


# Words that say nothing about WHAT the job is: level, schedule, punctuation.
_TITLE_NOISE = {"intern", "internship", "co", "op", "coop", "summer", "winter", "fall",
                "spring", "the", "of", "and", "or", "a", "an", "in", "for", "to",
                "i", "ii", "iii", "iv", "new", "grad", "graduate", "junior", "senior",
                "sr", "staff", "lead", "principal", "level", "entry"}


def _title_tokens(title: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z][a-z+#.]*", title.lower()):
        w = w.rstrip(".")
        if w in _TITLE_NOISE or len(w) < 2:
            continue
        # crude stem so "engineering" meets "engineer" and "systems" meets "system"
        for suf in ("ing", "s"):
            if len(w) > 5 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def _title_match(targets: list[str], title: str) -> tuple[float, float]:
    """Best word-overlap between the posting title and any target title.

    A substring test could not see that "Machine Learning Engineer Intern"
    describes "Machine Learning Infrastructure Engineer": word order differs
    and the level suffix is not in the posting. Every title therefore scored
    the no-match floor and the whole board fell under the threshold. Returns
    (score, overlap) so the caller can also tell "no shared word at all".
    """
    posting = _title_tokens(title)
    if not posting:
        return 0.45, 0.0
    best = 0.0
    for t in targets:
        want = _title_tokens(t)
        if not want:
            continue
        if t in title.lower():
            return 1.0, 1.0
        best = max(best, len(want & posting) / len(want))
    return round(0.45 + 0.55 * best, 3), best


def fit_score(profile: Profile, post: JobPost) -> float:
    """Cheap lexical fit, used only to rank and to filter obvious mismatches.

    Deliberately generous. The evidence says returns to additional applications
    are roughly constant *inside* your genuine match set and collapse outside
    it, and that interview odds plateau once you meet about half a posting's
    listed requirements. Self-screening at 80% costs real interviews.
    """
    text = f"{post.title} {post.description}".lower()
    if not text.strip():
        return 0.5

    titles = [t.lower() for t in profile.target_titles]
    if titles:
        title_score, overlap = _title_match(titles, post.title)
    else:
        title_score, overlap = 0.6, 1.0

    if post.description.strip():
        skills = [s.lower() for items in profile.skills.values() for s in items]
        hits = sum(1 for s in skills if s and s in text)
        # The bar for "full skill match" is a property of the posting, not of
        # how thorough the candidate was. Scaling it with the profile's size
        # meant a 69-skill profile needed 24 hits in one description where an
        # 11-skill profile needed 6 -- the richer profile scored lower on the
        # same job, and every posting on a board came in under the threshold.
        skill_score = min(1.0, hits / min(max(6, len(skills) * 0.35), 8)) if skills else 0.5
        score = 0.6 * skill_score + 0.4 * title_score
    else:
        # Workday's search API returns postings with no description body. Scoring
        # skills against a bare title always yields zero, which put every Workday
        # posting under the default 0.5 threshold -- silently filtering out the
        # ATS that accounts for most of the real apply volume. Score on the title
        # alone instead of penalising the posting for data the source never sent.
        score = title_score

    # A posting whose title shares no word at all with any target is almost
    # never what the candidate meant. Without this, a support role whose
    # description happened to mention Python and Linux scored the same as the
    # ML infrastructure job next to it.
    if titles and overlap == 0.0:
        score *= 0.6

    # Seniority sanity. Applying to Staff and Principal roles with under two
    # years of experience is the undirected-volume case the evidence says has
    # collapsing returns -- and it is a knockout on years-of-experience anyway.
    yrs = profile.total_years_experience
    t = post.title.lower()
    SENIOR = (("principal", 8), ("distinguished", 10), ("staff", 6),
              ("director", 10), ("head of", 10), ("vp ", 12),
              ("senior", 3), ("sr.", 3), ("lead ", 5), ("manager", 5))
    for word, needs in SENIOR:
        if word in t and yrs < needs:
            shortfall = min(1.0, (needs - yrs) / max(needs, 1))
            score *= max(0.15, 1.0 - shortfall)
            break

    # Intern and new-grad postings are a strong positive for a current student.
    if any(k in t for k in ("intern", "new grad", "new-grad", "university", "entry level")):
        if profile.education and not profile.education[0].completed:
            score = min(1.0, score * 1.35)

    return round(score, 3)


async def _enter_embedded_form(page: Any) -> str | None:
    """Follow a Greenhouse form embedded in a company careers page into its own tab.

    Company sites (nuro.ai/careersitem?gh_jid=...) host the application in an
    iframe from job-boards.greenhouse.io/embed/job_app. Nothing here walks
    frames: the DOM extract saw one control on the outer page and vision then
    reported seventeen fields with no selector, none of which get_by_label could
    reach across the frame boundary -- 1 of 17 filled. The embed URL renders the
    same form standalone, so navigating into it fixes extract, capture, fill and
    verify at once instead of teaching each of them about frames.
    """
    frame = page.locator("iframe[src*='greenhouse.io/embed/job_app']").first
    if not await frame.count():
        return None
    src = await frame.get_attribute("src")
    if not src:
        return None
    log.info("apply.embedded_form", src=src.split("?")[0])
    await page.goto(src, wait_until="domcontentloaded")
    await cap.settle(page, quiet_ms=900)
    return src


def resume_pdf_none() -> Path:
    """No PDF was produced; the caller only reads the failure."""
    return Path()


def _keep_a_copy(pdf: Path, post: JobPost) -> None:
    """Drop a named copy where the candidate can find it.

    The audit copy is called resume.pdf inside a directory named after a job
    id, which is unreadable when you want to look at what was sent. Set
    JOBBOT_RESUME_DIR to get "Nuro - Software Engineer AI Platform - Intern.pdf"
    somewhere useful instead.
    """
    dest_dir = os.environ.get("JOBBOT_RESUME_DIR")
    if not dest_dir:
        return
    safe = re.sub(r"[^\w .,&()-]", "", f"{post.company} - {post.title}")[:120].strip()
    try:
        d = Path(dest_dir).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, d / f"{safe}.pdf")
    except OSError as exc:
        # A copy for the candidate's convenience must never fail an application.
        log.warning("resume.copy_failed", error=str(exc)[:120])


class Orchestrator:
    def __init__(self, profile: Profile, session: BrowserSession, llm: LLMClient,
                 tracker: Tracker, config: RunConfig | None = None) -> None:
        self.profile = profile
        self.session = session
        self.llm = llm
        self.tracker = tracker
        self.cfg = config or RunConfig()
        self.lessons_path = self.cfg.data_dir / "lessons.jsonl"
        self._last_ats_score = 0.0
        self.answer_log = AnswerLog(self.cfg.data_dir / "answers.csv")

    # -- helpers ----------------------------------------------------------

    def _audit_dir(self, post: JobPost) -> Path:
        safe = post.job_id.replace(":", "_").replace("/", "_")
        d = self.cfg.data_dir / "applications" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _record_lessons(self, post: JobPost, lessons: list[dict[str, str]]) -> None:
        if not lessons:
            return
        self.lessons_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lessons_path, "a", encoding="utf-8") as fh:
            for l in lessons:
                fh.write(json.dumps({
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "ats": post.ats.value, "company": post.company,
                    "job_id": post.job_id, **l,
                }) + "\n")

    def preflight(self) -> list[str]:
        """Everything that must be set before an autonomous run may start.

        Identity is checked here rather than at save time: a half-filled draft
        is a normal intermediate state, a half-filled submission is not.
        """
        return (self.profile.missing_identity()
                + self.profile.missing_legally_significant())

    # -- one application --------------------------------------------------

    async def apply_to(self, post: JobPost) -> ApplicationResult:
        audit = self._audit_dir(post)
        shots = audit / "screenshots"
        row = Application(
            job_id=post.job_id, company=post.company, title=post.title,
            ats=post.ats.value, job_url=post.url, apply_url=post.url,
            location=post.location, remote=str(post.remote),
            posted_at=post.posted_at.isoformat() if post.posted_at else "",
            source=post.ats.value, audit_dir=str(audit), screenshots_dir=str(shots),
            status=Status.DISCOVERED.value,
        )
        self.tracker.upsert(row)

        if self.tracker.already_applied(post.job_id):
            return ApplicationResult(post.job_id, "skipped", "already applied")

        try:
            async with self.session.tab(
                    post.job_id,
                    keep_open_on=(HaltWithTabOpen,) if self.cfg.persist_until_submitted
                    else ()) as page:
                r = await self._apply_with_retries(page, post, audit, shots)
                if r.status == Status.NEEDS_HUMAN.value and not page.is_closed():
                    # The form is filled and live. Closing it would discard
                    # that work; leave it for the candidate to finish or fix.
                    self.session.keep(page, f"{post.company}: {post.title}"[:80])
                return r
        except HaltWithTabOpen:
            # Deliberate: carries the open tab up to the caller untouched.
            raise
        except Exception as exc:  # noqa: BLE001
            # Only reached when the tab itself could not be leased.
            log.error("apply.no_tab", job_id=post.job_id, error=str(exc)[:200])
            self.tracker.update(post.job_id, status=Status.FAILED.value,
                                error=str(exc)[:300])
            return ApplicationResult(post.job_id, Status.FAILED.value,
                                     f"tab died: {str(exc)[:180]}")
        finally:
            await self.session.reap_orphans()

    async def _apply_with_retries(self, page: Any, post: JobPost, audit: Path,
                                  shots: Path) -> ApplicationResult:
        """Work one application in one tab until it settles.

        A crash used to close the tab and file the job as failed after a
        single try. Most of what goes wrong mid-form is transient -- a model
        call that 400s or times out, a menu that did not open, a navigation
        that raced the page -- and the tab is the expensive thing. So the
        loop stays in the tab: each attempt re-navigates and re-fills (the
        filler verifies every write, so a half-filled form is fine), with
        backoff between tries. It stops on a settled result, on the attempt
        budget, or when the tab itself is gone.
        """
        jid = post.job_id
        budget = _attempt_budget(self.cfg)
        r: ApplicationResult | None = None
        for attempt in range(1, budget + 1):
            crashed = False
            try:
                r = await self._apply_in_tab(page, post, audit, shots)
            except HaltWithTabOpen:
                raise
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()[-1200:]
                log.error("apply.crashed", job_id=jid, attempt=attempt, of=budget,
                          error=str(exc)[:200])
                with (audit / "error.txt").open("a", encoding="utf-8") as fh:
                    fh.write(f"--- attempt {attempt}\n{tb}\n")
                r = ApplicationResult(jid, Status.FAILED.value, str(exc)[:200])
                crashed = True
                if page.is_closed():
                    self.tracker.update(jid, status=Status.FAILED.value,
                                        error=str(exc)[:300])
                    return ApplicationResult(jid, Status.FAILED.value,
                                             f"tab died: {str(exc)[:180]}")
            # An exception is transient by construction (a 503, a timeout, a
            # menu race) and is always retried; _worth_retrying reads the
            # *reason* text, and a Gemini 503 body says "try again later" --
            # the exact phrase that marks Oracle's lockout page as settled.
            if not crashed and (r.status in _SETTLED_OK or not _worth_retrying(r)):
                return r
            if r.status == Status.NEEDS_HUMAN.value and attempt >= MAX_UNCLEAN_PASSES:
                log.warning("apply.unclean_after_passes", job_id=jid, passes=attempt)
                break
            if attempt >= budget:
                break
            delay = _retry_delay(attempt)
            log.warning("apply.retrying_in_tab", job_id=jid, company=post.company,
                        attempt=attempt + 1, of=budget, after=r.status,
                        reason=r.reason[:120], in_s=round(delay))
            self.tracker.update(jid, status=Status.FILLING.value,
                                error=f"retry {attempt + 1}/{budget}: {r.reason[:200]}")
            await asyncio.sleep(delay)
        assert r is not None
        self.tracker.update(jid, status=r.status, error=r.reason[:300])
        return r


    async def _resume_for(self, page: Any, post: JobPost, audit: Path,
                          jid: str) -> tuple[Path, str, ApplicationResult | None]:
        """The PDF to upload, and the GitHub project to cite alongside it.

        With cfg.standard_resume set, this is one file copy: the candidate's own
        resume goes up unchanged, no model involved. That is the point -- a
        per-application rewrite costs four minutes and half a dozen model calls,
        and a resume the candidate did not write is one they cannot stand behind
        in the interview it wins.
        """
        if self.cfg.standard_resume:
            resume_pdf = audit / "resume.pdf"
            shutil.copy2(self.cfg.standard_resume, resume_pdf)
            log.info("resume.standard", job_id=jid, src=str(self.cfg.standard_resume))
            self.tracker.update(jid, status=Status.PREPARED.value,
                                resume_path=str(resume_pdf),
                                match_score=fit_score(self.profile, post))
            return resume_pdf, "", None

        # --- expensive work starts here ----------------------------------
        project_url = ""
        extra_project = None
        if self.cfg.make_github_project:
            try:
                extra_project, project_url = await asyncio.to_thread(
                    self._build_project, post, audit)
            except Exception as exc:  # noqa: BLE001
                log.warning("apply.project_failed", job_id=jid, error=str(exc)[:200])

        tailored = await asyncio.to_thread(
            tailor, self.llm, self.profile,
            job_title=post.title, company=post.company,
            job_description=post.description, extra_project=extra_project)

        # Per-application refinement: critique as this role's hiring manager,
        # revise, repeat. Bounded, stops on "ship", and any revision that
        # smuggles in a new fact is discarded rather than printed.
        tailored, critique_history = await asyncio.to_thread(
            refine, self.llm, self.profile, tailored,
            job_title=post.title, company=post.company,
            job_description=post.description,
            max_rounds=self.cfg.max_resume_rounds)
        (audit / "resume_critique.json").write_text(json.dumps(critique_history, indent=2), encoding="utf-8")
        self._last_ats_score = float(
            next((h.get("final_ats_score", 0.0) for h in reversed(critique_history)
                  if "final_ats_score" in h), 0.0))

        dropped = sanitize_skills(self.profile, tailored)
        if dropped:
            (audit / "skills_removed.txt").write_text("\n".join(dropped), encoding="utf-8")
        fabrications = fabrication_check(self.profile, tailored)
        if fabrications:
            # A resume that overstates is worse than no application.
            log.error("apply.fabrication_detected", job_id=jid, problems=fabrications[:4])
            (audit / "fabrication_report.txt").write_text("\n".join(fabrications), encoding="utf-8")
            self.tracker.update(jid, status=Status.NEEDS_HUMAN.value,
                                error="resume fabrication check failed")
            return resume_pdf_none(), "", ApplicationResult(
                jid, Status.NEEDS_HUMAN.value,
                "fabrication check failed", flagged=fabrications)

        resume_pdf = audit / "resume.pdf"
        await render_one_page(page, tailored, resume_pdf)
        _keep_a_copy(resume_pdf, post)
        (audit / "resume_content.json").write_text(json.dumps(tailored, indent=2), encoding="utf-8")
        self.tracker.update(jid, status=Status.PREPARED.value,
                            resume_path=str(resume_pdf), github_project_url=project_url,
                            match_score=fit_score(self.profile, post))
        return resume_pdf, project_url, None

    async def _apply_in_tab(self, page: Any, post: JobPost, audit: Path,
                            shots: Path) -> ApplicationResult:
        jid = post.job_id
        await page.goto(post.url, wait_until="domcontentloaded")
        await cap.settle(page, quiet_ms=900)
        form_url = await _enter_embedded_form(page) or post.url

        det = detect(page.url, None)
        log.info("apply.start", job_id=jid, ats=det.ats.value, company=post.company)

        # Oracle puts an email address and a terms checkbox between the
        # posting and the form. It is not an account wall -- no password, no
        # verification code -- so it is handled here rather than below.
        if det.ats is ATS.ORACLE:
            ok, detail = await orc.start_application(
                page, self.profile.identity.email_str,
                gmail_enabled=self.cfg.gmail_enabled)
            if not ok:
                self.tracker.update(jid, status=Status.UNREACHABLE.value,
                                    error=detail[:250])
                return ApplicationResult(jid, Status.UNREACHABLE.value,
                                         f"oracle gate: {detail}")

        # --- account wall (Workday and friends) --------------------------
        if det.ats in REQUIRES_ACCOUNT:
            if det.ats is ATS.WORKDAY:
                await wd.start_application(page)
                if await wd.needs_account(page):
                    res = await wd.ensure_account(
                        page, tenant=det.tenant or post.company,
                        email=self.profile.identity.email_str,
                        gmail_enabled=self.cfg.gmail_enabled)
                    if not res.signed_in:
                        self.tracker.update(jid, status=Status.UNREACHABLE.value,
                                            error=res.error[:250])
                        return ApplicationResult(jid, Status.UNREACHABLE.value,
                                                 f"account wall: {res.error}")
                    log.info("apply.account_ok", created=res.created,
                             emailed_code=res.needed_email_code)
                    # Signing in returns to the posting, not to the form.
                    # Blackstone signed in cleanly and then reported "form
                    # still gated behind sign-in" on three attempts, because
                    # nothing clicked Apply again once we were through the
                    # door.
                    if await wd.needs_account(page):
                        log.info("apply.reentering_after_signin", job_id=jid)
                        await wd.start_application(page)
                        await cap.settle(page, quiet_ms=900)
            else:
                self.tracker.update(jid, status=Status.UNREACHABLE.value,
                                    error=f"{det.ats.value} requires an account; no adapter yet")
                return ApplicationResult(jid, Status.UNREACHABLE.value,
                                         f"{det.ats.value} account wall unsupported")

        # --- checkpoint 1: read the form ---------------------------------
        form, _ = await ck.checkpoint_parse(page, self.llm, shots)
        (audit / "form.json").write_text(json.dumps(
            {"submit": form.submit_label, "step": form.step,
             "fields": [f.to_prompt_dict() for f in form.fields]}, indent=2), encoding="utf-8")

        # Vision decides whether a sign-in gate is up, and on Blackstone's form
        # it answered differently on three consecutive attempts -- True, False,
        # True -- for the same fourteen-field page we were already signed into.
        # The DOM is evidence rather than a guess: a page offering several
        # fields that are not credentials is the application, not the gate.
        real_fields = [f for f in form.fields
                       if not _AUTH_FIELD.search(f.label or "")]
        if form.requires_account and len(real_fields) < 3:
            self.tracker.update(jid, status=Status.UNREACHABLE.value,
                                error="form still gated behind sign-in")
            return ApplicationResult(jid, Status.UNREACHABLE.value, "sign-in gate")
        if form.requires_account:
            log.info("apply.gate_overruled_by_dom", job_id=jid,
                     fields=len(real_fields))

        if not form.fields:
            # Distinguish "we could not read the form" from "there is no form".
            # AbbVie's SmartRecruiters posting offers only "Apply With Indeed":
            # a third-party account handoff, not a form to fill. Reported as
            # "no answerable fields found" it read like a parser bug and was
            # retried on every pass.
            third_party = await page.evaluate(_THIRD_PARTY_APPLY_JS)
            if third_party:
                reason = f"only third-party apply offered: {', '.join(third_party)}"
                log.warning("apply.third_party_only", job_id=jid, options=third_party)
                self.tracker.update(jid, status=Status.UNREACHABLE.value, error=reason[:250])
                return ApplicationResult(jid, Status.UNREACHABLE.value, reason)
            self.tracker.update(jid, status=Status.FAILED.value,
                                error="no answerable fields found")
            return ApplicationResult(jid, Status.FAILED.value, "no fields")

        # --- knockout pre-scan, BEFORE any expensive work ----------------
        # Off the event loop like every other model call: while this blocked,
        # nothing else could run -- including Playwright's own connection.
        knockouts, skip = await asyncio.to_thread(
            ck.knockout_scan, self.llm, self.profile, form, post.title)
        if skip and knockouts:
            reason = "; ".join(f"{k['label'][:40]}={k['honest_answer'][:20]}" for k in knockouts[:3])
            self.tracker.update(jid, status=Status.KNOCKOUT_FAIL.value,
                                knockout_reason=reason[:300],
                                questions_total=len(form.fields))
            log.info("apply.knockout_skip", job_id=jid, reason=reason[:120])
            return ApplicationResult(jid, Status.KNOCKOUT_FAIL.value, reason)

        # --- resume ------------------------------------------------------
        # Where to come back to. Getting here may have cost several steps --
        # Oracle's email gate, a Workday sign-in -- and form_url still points
        # at the job description from before any of that. Returning to it
        # threw the whole gate away: the first American Express application
        # reached the form, prepared a resume, and then went back to the
        # advert and reported zero fields.
        form_url = page.url or form_url
        resume_pdf, project_url, failure = await self._resume_for(page, post, audit, jid)
        if failure is not None:
            return failure

        # Re-navigate: rendering the PDF took this tab to a file:// URL.
        await page.goto(form_url, wait_until="domcontentloaded")
        await cap.settle(page, quiet_ms=800)
        if det.ats in REQUIRES_ACCOUNT and det.ats is ATS.WORKDAY:
            await wd.start_application(page)
        elif det.ats is ATS.ORACLE:
            # Returning to a section URL can bounce back through the gate.
            await orc.start_application(page, self.profile.identity.email_str,
                                        gmail_enabled=self.cfg.gmail_enabled)
        form, pc2 = await ck.checkpoint_parse(page, self.llm, shots)

        # Read every option list before composing a single answer: a control
        # that hides its choices behind a click made every stage downstream
        # guess, and the guesses were wrong in ways no log could explain.
        from jobbot.forms.fill import discover_options
        await discover_options(page, form)

        # --- answers ------------------------------------------------------
        det_answers, leftover = deterministic_answers(
            self.profile, form,
            published_salary=(post.salary_min, post.salary_max))
        # A file input is not a question. Sending it to the model produced a
        # needs_human "answer" for the Attach field, which then counted as
        # answered -- so the resume was never attached to it.
        leftover = [f for f in leftover if f.kind is not FieldKind.FILE]
        llm_answers = await asyncio.to_thread(
            model_answers, self.llm, self.profile, leftover,
            job_context=f"{post.title} at {post.company}\n\n{post.description[:4000]}",
            images=pc2.tiles, aria=pc2.aria)
        answers = det_answers + llm_answers

        # Second pass: any REQUIRED, non-legal field still unanswered gets one
        # more attempt on its own. A required blank fails validation at submit,
        # after the expensive work is already done.
        answered_ids = {a.field_id for a in answers if a.submittable}
        retry = [f for f in leftover
                 if f.required and f.field_id not in answered_ids
                 and not f.legally_significant
                 and f.kind is not FieldKind.FILE]
        if retry:
            log.info("apply.retry_required", count=len(retry),
                     labels=[f.label[:40] for f in retry])
            extra = await asyncio.to_thread(
                model_answers, self.llm, self.profile, retry,
                job_context=(f"{post.title} at {post.company}\n\n"
                             f"{post.description[:4000]}\n\n"
                             "These required fields were left blank on the first pass. "
                             "Answer each one now, grounded in the profile."),
                images=pc2.tiles, aria=pc2.aria)
            got = {a.field_id for a in extra if a.submittable}
            answers = [a for a in answers if a.field_id not in got] + extra

        blocked = [a for a in answers if a.needs_human]
        legal_blocked = [a for a in blocked if "legally significant" in a.blocked_reason]
        if legal_blocked:
            reasons = [a.blocked_reason for a in legal_blocked]
            (audit / "needs_human.txt").write_text("\n".join(reasons), encoding="utf-8")
            self.tracker.update(jid, status=Status.NEEDS_HUMAN.value,
                                questions_total=len(form.fields),
                                questions_flagged=len(blocked),
                                error="; ".join(reasons)[:300])
            log.warning("apply.halted_legal", job_id=jid, count=len(legal_blocked))
            return ApplicationResult(jid, Status.NEEDS_HUMAN.value,
                                     "legally significant answer missing from profile",
                                     flagged=reasons)

        # --- fill ---------------------------------------------------------
        self.tracker.update(jid, status=Status.FILLING.value)
        # Always attach the resume. Nothing upstream emits an answer for a file
        # field, so without this the PDF is generated and then never uploaded --
        # which the verifier correctly refuses to submit.
        resume_slots: list[FormField] = []
        for f in form.fields:
            if f.kind is not FieldKind.FILE:
                continue
            if any(a.field_id == f.field_id for a in answers):
                continue
            if re.search(r"cover[\s_-]*letter|transcript|portfolio",
                         f"{f.label} {f.field_id}", re.I):
                # Not a resume slot. Leave it empty rather than upload the
                # wrong document under a heading the reviewer will read.
                continue
            resume_slots.append(f)

        # Attach to every required resume slot, not just the first. A form that
        # lists an optional "Resume / CV" before the required "Resume*" left the
        # required one empty, and the page refused to submit with "Please select
        # a file". If none is marked required, the first slot is the resume slot.
        required_slots = [f for f in resume_slots if f.required]
        for f in (required_slots or resume_slots[:1]):
            answers.append(ProposedAnswer(
                f.field_id, str(resume_pdf), AnswerSource.PROFILE, 1.0,
                "tailored resume for this role"))

        by_id = {f.field_id: f for f in form.fields}
        filled = 0
        failed: list[FormField] = []
        for a in answers:
            f = by_id.get(a.field_id)
            if f is None or not a.submittable:
                continue
            if await apply_answer(page, f, a, resume_path=resume_pdf):
                filled += 1
            else:
                failed.append(f)
        log.info("apply.filled", job_id=jid, filled=filled, total=len(form.fields))

        # A dropdown's options often exist only once it is open, so the first
        # answer was composed without them: "Yes" for a list of sentences,
        # today's date for a list of month-year choices. Filling records what
        # the control actually offered, so anything that failed and now has
        # options is worth one more answer -- this time with the list in hand.
        blind = [f for f in failed if f.options and f.required]
        if blind:
            log.info("apply.reanswer_with_options", count=len(blind),
                     labels=[f.label[:40] for f in blind])
            second = await asyncio.to_thread(
                model_answers, self.llm, self.profile, blind,
                job_context=(f"{post.title} at {post.company}\n\n"
                             f"{post.description[:3000]}\n\n"
                             "Each field below now lists the exact options the "
                             "control offers. Choose one of them verbatim."),
                images=pc2.tiles, aria=pc2.aria)
            for a in second:
                f = by_id.get(a.field_id)
                if f is not None and a.submittable and await apply_answer(
                        page, f, a, resume_path=resume_pdf):
                    filled += 1
                    answers = [x for x in answers if x.field_id != a.field_id] + [a]
            log.info("apply.filled_after_reanswer", job_id=jid, filled=filled)

        def dump_answers() -> None:
            (audit / "answers.json").write_text(json.dumps([
                {
                    "field_id": a.field_id,
                    "label": (by_id[a.field_id].label if a.field_id in by_id else ""),
                    "kind": (by_id[a.field_id].kind.value if a.field_id in by_id else ""),
                    "required": (by_id[a.field_id].required if a.field_id in by_id else False),
                    "value": a.value if not isinstance(a.value, Path) else str(a.value),
                    "source": a.source.value,
                    "confidence": a.confidence,
                    "rationale": a.rationale,
                    "needs_human": a.needs_human,
                    "blocked_reason": a.blocked_reason,
                }
                for a in answers
            ], indent=2, default=str), encoding="utf-8")
            self.answer_log.record(
                job_id=jid, company=post.company, title=post.title,
                ats=post.ats.value, job_url=post.url,
                answers=json.loads((audit / "answers.json").read_text(encoding="utf-8")))

        dump_answers()

        # --- checkpoint 2 + healing ---------------------------------------
        verification, rounds = await ck.heal(
            page, self.llm, self.profile, form, answers, shots,
            max_rounds=self.cfg.max_heal_rounds, resume_path=resume_pdf)
        # The healer rewrites answers in place, so re-record. Written once before
        # the loop as well, so a crash mid-heal still leaves a ledger behind.
        dump_answers()
        (audit / "verification.json").write_text(json.dumps({
            "ready": verification.ready_to_submit,
            "summary": verification.summary,
            "issues": [i.__dict__ for i in verification.issues],
            "unfilled_required": verification.unfilled_required,
            "validation_errors": verification.validation_errors,
            "heal_rounds": rounds,
        }, indent=2), encoding="utf-8")

        # Workday is a five-step wizard -- My Information, My Experience,
        # Application Questions, Voluntary Disclosures, Review -- and this
        # filled the first page and then looked for a submit button that only
        # exists on the last one. wd.advance() and wd.is_final_step() have been
        # in the adapter all along with nothing calling them, which is why no
        # Workday application has ever completed.
        wizard = {ATS.WORKDAY: wd, ATS.ORACLE: orc}.get(det.ats)
        if wizard is not None:
            for step in range(1, MAX_WIZARD_STEPS + 1):
                if await wizard.is_final_step(page):
                    log.info("apply.wizard_final_step", job_id=jid, step=step)
                    break
                moved, label = await wizard.advance(page)
                if not moved:
                    log.info("apply.wizard_stuck", job_id=jid, step=step,
                             detail=label[:80])
                    break
                await cap.settle(page, quiet_ms=1200)
                log.info("apply.wizard_advanced", job_id=jid, step=step,
                         now=label[:60])

                form, pc_step = await ck.checkpoint_parse(page, self.llm, shots)
                if not form.fields:
                    continue
                await discover_options(page, form)
                det_step, leftover_step = deterministic_answers(
                    self.profile, form,
                    published_salary=(post.salary_min, post.salary_max))
                leftover_step = [f for f in leftover_step
                                 if f.kind is not FieldKind.FILE]
                llm_step = await asyncio.to_thread(
                    model_answers, self.llm, self.profile, leftover_step,
                    job_context=f"{post.title} at {post.company}",
                    images=pc_step.tiles, aria=pc_step.aria)
                step_answers = det_step + llm_step

                by_id_step = {f.field_id: f for f in form.fields}
                for f in form.fields:
                    if f.kind is FieldKind.FILE and not any(
                            a.field_id == f.field_id for a in step_answers) and not re.search(
                            r"cover[\s_-]*letter|transcript|portfolio",
                            f"{f.label} {f.field_id}", re.I):
                        step_answers.append(ProposedAnswer(
                            f.field_id, str(resume_pdf), AnswerSource.PROFILE, 1.0,
                            "resume for this role"))
                for a in step_answers:
                    f = by_id_step.get(a.field_id)
                    if f is not None and a.submittable:
                        await apply_answer(page, f, a, resume_path=resume_pdf)
                answers.extend(step_answers)

                verification, rounds = await ck.heal(
                    page, self.llm, self.profile, form, step_answers, shots,
                    max_rounds=max(2, self.cfg.max_heal_rounds // 2),
                    resume_path=resume_pdf)
                dump_answers()

        self.tracker.update(jid, questions_total=len(form.fields),
                            questions_answered=filled,
                            questions_flagged=len(blocked), heal_rounds=rounds)

        if not verification.ready_to_submit or verification.blockers:
            self.tracker.update(jid, status=Status.NEEDS_HUMAN.value,
                                error=f"{len(verification.blockers)} unresolved blockers")
            (audit / "blockers.txt").write_text(
                "\n".join(f"{i.label}: {i.problem}" for i in verification.blockers), encoding="utf-8")
            # A question only the candidate can answer is not something to sit
            # in front of. Waiting on one halts every other application behind
            # it, and no amount of retrying will produce a legal attestation
            # nobody has given us. Record it, move on, come back when it is
            # answered. Anything else keeps its tab.
            by_id = {f.field_id: f for f in form.fields}
            needs_candidate = all(
                (by_id.get(i.field_id) is not None
                 and by_id[i.field_id].profile_key in LEGALLY_SIGNIFICANT
                 and not self.profile.can_answer(by_id[i.field_id].profile_key))
                for i in verification.blockers)
            if self.cfg.persist_until_submitted and not needs_candidate:
                # Leave it exactly as it stands: the form filled, the tab open,
                # the page live. Closing it would discard the work and the next
                # attempt would start from an empty form.
                log.error("apply.halted_open", job_id=jid,
                          blockers=[i.label[:60] for i in verification.blockers])
                raise HaltWithTabOpen(jid, verification.blockers)
            reason = "verification not clean"
            if needs_candidate:
                log.warning("apply.awaiting_candidate", job_id=jid,
                            questions=[i.label[:60] for i in verification.blockers])
                reason = "awaiting candidate: " + "; ".join(
                    i.label[:40] for i in verification.blockers[:4])
            return ApplicationResult(jid, Status.NEEDS_HUMAN.value,
                                     reason, heal_rounds=rounds,
                                     flagged=[i.problem for i in verification.blockers],
                                     resume_path=str(resume_pdf), project_url=project_url)

        if self.cfg.dry_run:
            log.info("apply.dry_run_stop", job_id=jid)
            self.tracker.update(jid, status=Status.PREPARED.value,
                                notes="dry run: verified, not submitted")
            return ApplicationResult(jid, "dry_run", "verified but not submitted",
                                     resume_path=str(resume_pdf), project_url=project_url,
                                     heal_rounds=rounds)

        # --- submit --------------------------------------------------------
        submitted_click, click_error = False, ""
        selectors = (f"button:has-text({q(form.submit_label)})",
                     "[data-automation-id='bottom-navigation-submit-button']",
                     "button[type=submit]", "input[type=submit]")
        # A single pass was the reason two filled applications were never
        # sent. The page is still settling when the last field is written --
        # a textarea grows to fit an essay and everything below it moves --
        # and a humanised click refuses a target whose position is still
        # changing. The button is not missing, it is moving, so wait for it
        # to stop rather than give up on the first refusal.
        for attempt in range(SUBMIT_CLICK_TRIES):
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.scroll_into_view_if_needed(timeout=4000)
                        await loc.click(timeout=8000)
                        submitted_click = True
                        break
                except Exception as exc:  # noqa: BLE001
                    click_error = str(exc)[:120]
                    continue
            if submitted_click:
                break
            if attempt < SUBMIT_CLICK_TRIES - 1:
                log.debug("apply.submit_click_retry", job_id=jid,
                          attempt=attempt + 1, detail=click_error[:80])
                await cap.settle(page, quiet_ms=1500)
                await asyncio.sleep(2)

        # Whether the button was ever clicked was worked out here and then
        # thrown away, so a submit that never happened and a submit that
        # happened and showed nothing both came out as "no confirmation
        # observed" -- two different faults, one indistinguishable message.
        if not submitted_click:
            detail = f"no clickable submit button ({click_error or 'none matched'})"
            log.error("apply.submit_not_clicked", job_id=jid,
                      label=form.submit_label[:40], detail=detail[:120])
            self.tracker.update(jid, status=Status.NEEDS_HUMAN.value, error=detail)
            raise HaltWithTabOpen(jid, verification.blockers)

        log.info("apply.submit_clicked", job_id=jid, label=form.submit_label[:40])
        await _settle_after_submit(page)

        # --- checkpoint 3: one call, did it actually go through? ----------
        outcome, _ = await ck.checkpoint_outcome(page, self.llm, shots)
        self._record_lessons(post, outcome.lessons)

        if outcome.submitted:
            self.tracker.mark_submitted(jid, outcome.evidence)
        elif _looks_rejected(outcome.evidence, outcome.errors):
            # Not ambiguous: the page said why it refused. Dedalus Labs
            # answered a submit with "Your form needs corrections -- Missing
            # entry for required field", and recording that as SUBMITTED made
            # already_applied skip the job forever over an application that
            # was never sent.
            detail = (outcome.evidence or "; ".join(outcome.errors))[:250]
            log.warning("apply.rejected_at_submit", job_id=jid, detail=detail[:120])
            self.tracker.update(jid, status=Status.NEEDS_HUMAN.value,
                                error=detail,
                                notes="submit refused by the form; not sent")
        else:
            # A click is not confirmation. Without positive evidence we record
            # SUBMITTED (not CONFIRMED) if we clicked, so a human can check --
            # and never claim success we cannot see.
            self.tracker.update(
                jid,
                status=Status.SUBMITTED.value if submitted_click else Status.FAILED.value,
                error="; ".join(outcome.errors)[:250] or "no confirmation observed",
                notes="clicked submit but no confirmation seen" if submitted_click else "")

        result = ApplicationResult(
            jid, Status.CONFIRMED.value if outcome.submitted else Status.SUBMITTED.value,
            outcome.evidence or "no confirmation text",
            resume_path=str(resume_pdf), project_url=project_url,
            submitted=outcome.submitted, evidence=outcome.evidence,
            heal_rounds=rounds, lessons=outcome.lessons)

        if self.cfg.notify:
            try:
                rel = reliability_score(
                    submitted=bool(submitted_click), evidence=outcome.evidence,
                    heal_rounds=rounds, flagged=len(blocked),
                    total_fields=len(form.fields),
                    verified=verification.ready_to_submit)
                notify_send(Notification(
                    company=post.company, title=post.title, url=post.url,
                    match_score=fit_score(self.profile, post),
                    ats_score=self._last_ats_score,
                    reliability=rel, status=result.status,
                    evidence=outcome.evidence, answered=filled,
                    total_fields=len(form.fields), flagged=len(blocked),
                    heal_rounds=rounds, project_url=project_url,
                ), to=self.cfg.notify_to)
            except Exception as exc:  # noqa: BLE001
                # A notification is a report about work already done. It must
                # never change or mask the outcome it is reporting.
                log.warning("apply.notify_failed", job_id=jid, error=str(exc)[:160])

        return result

    # -- github project ---------------------------------------------------

    def _build_project(self, post: JobPost, audit: Path) -> tuple[dict | None, str]:
        from jobbot.ghproj.auth import consent_record, get_identity
        from jobbot.ghproj.generate import design_project, validate_plan
        from jobbot.ghproj.publish import publish, smoke_test

        ident = get_identity()
        if not consent_record():
            log.warning("project.skipped_no_consent")
            return None, ""

        plan = design_project(
            self.llm, self.profile, job_title=post.title, company=post.company,
            job_description=post.description, team_context=post.department)

        problems = validate_plan(plan)
        if problems:
            log.warning("project.plan_rejected", problems=problems[:4])
            (audit / "project_rejected.txt").write_text("\n".join(problems), encoding="utf-8")
            return None, ""

        local = audit / "project"
        pub = publish(ident, plan, private=self.cfg.publish_project_private,
                      author_name=self.profile.identity.full_name,
                      author_email=self.profile.identity.email_str,
                      keep_local=local, dry_run=True)

        smoke = smoke_test(pub.local_path, plan.get("run_command"))
        (audit / "project_smoke.json").write_text(json.dumps(smoke, indent=2), encoding="utf-8")
        if not smoke["passed"]:
            log.warning("project.smoke_failed", output=smoke["output"][-200:])
            return None, ""

        if self.cfg.dry_run:
            # Creating a repository is public and not retractable, so it does not
            # belong in a run the user asked to stop before submitting. The plan
            # is still built and smoke-tested locally; only the push is withheld.
            # No URL is returned, so the resume never prints a link to a repo
            # that does not exist.
            log.info("project.dry_run_not_published", repo=plan["repo_name"],
                     local=str(local))
            return ({"name": plan["repo_name"],
                     "bullets": plan.get("resume_bullets", [])}, "")

        import shutil
        shutil.rmtree(local, ignore_errors=True)
        pub = publish(ident, plan, private=self.cfg.publish_project_private,
                      author_name=self.profile.identity.full_name,
                      author_email=self.profile.identity.email_str,
                      keep_local=local, dry_run=False)

        return ({"name": plan["repo_name"], "url": pub.url,
                 "bullets": plan.get("resume_bullets", [])}, pub.url)

    # -- the run ----------------------------------------------------------

    async def run(self, posts: list[JobPost], limit: int = 10) -> list[ApplicationResult]:
        missing = self.preflight()
        if missing:
            raise RuntimeError(
                "refusing to run: these legally significant answers are unset in the "
                f"profile and will never be guessed: {missing}"
            )

        applied_companies: dict[str, int] = {}
        for c in self.tracker.applied_companies():
            applied_companies[c] = applied_companies.get(c, 0) + 1

        queue: list[tuple[float, JobPost]] = []
        excluded = 0
        for p in posts:
            if self.tracker.already_applied(p.job_id):
                log.info("plan.dropped", why="already_applied", job_id=p.job_id,
                         company=p.company)
                continue
            # A question only the candidate can answer will not answer itself
            # between runs. Databricks asks a sanctions disclosure and cost
            # three minutes of every run reaching the same halt.
            row = self.tracker.get(p.job_id)
            if row is not None and row.status == Status.NEEDS_HUMAN.value \
                    and "legally significant" in (row.error or ""):
                log.info("plan.awaiting_candidate_answer", job_id=p.job_id,
                         company=p.company, question=(row.error or "")[:70])
                continue
            if self.profile.excludes(p.company):
                excluded += 1
                continue
            # Never queue a posting we cannot apply to on its own ATS.
            # An unresolved aggregator listing means the only route is the
            # aggregator's own apply flow -- and LinkedIn Easy Apply is the one
            # platform with documented account bans for exactly that. Skip it
            # rather than fall back to it.
            if p.ats in (ATS.UNKNOWN, ATS.LINKEDIN):
                self.tracker.upsert(Application(
                    job_id=p.job_id, company=p.company, title=p.title,
                    ats=p.ats.value, job_url=p.url,
                    status=Status.UNREACHABLE.value,
                    error="no ATS apply URL resolved; not applying via the aggregator"))
                log.info("plan.dropped", why="no_ats", job_id=p.job_id,
                         company=p.company, url=p.url[:80])
                continue
            g = ghost_score(p, posts)
            if g > self.cfg.max_ghost_score:
                log.info("plan.dropped", why="ghost", job_id=p.job_id,
                         company=p.company, score=round(g, 2))
                self.tracker.upsert(Application(
                    job_id=p.job_id, company=p.company, title=p.title, ats=p.ats.value,
                    job_url=p.url, status=Status.GHOST_SUSPECTED.value, ghost_score=str(g)))
                continue
            m = fit_score(self.profile, p)
            if m < self.cfg.min_match_score:
                log.info("plan.dropped", why="fit", job_id=p.job_id,
                         company=p.company, score=round(m, 2),
                         floor=self.cfg.min_match_score)
                self.tracker.upsert(Application(
                    job_id=p.job_id, company=p.company, title=p.title, ats=p.ats.value,
                    job_url=p.url, status=Status.FILTERED_OUT.value, match_score=str(m)))
                continue
            if applied_companies.get(p.company.lower(), 0) >= self.cfg.per_company_cap:
                log.info("plan.dropped", why="company_cap", job_id=p.job_id,
                         company=p.company)
                continue
            queue.append((m, p))

        # Internships first, then new-grad, then full-time IC, then senior --
        # and best fit within each tier. Ordering rather than filtering, so a
        # strong full-time match is still reached once the interns run out.
        queue.sort(key=lambda x: (role_tier(x[1]), -x[0]))
        by_tier: dict[int, int] = {}
        for _, p_ in queue:
            by_tier[role_tier(p_)] = by_tier.get(role_tier(p_), 0) + 1
        log.info("run.queued", candidates=len(posts), queued=len(queue), limit=limit,
                 excluded=excluded,
                 intern=by_tier.get(TIER_INTERN, 0), newgrad=by_tier.get(TIER_NEWGRAD, 0),
                 fulltime=by_tier.get(TIER_FULLTIME, 0), senior=by_tier.get(TIER_SENIOR, 0))

        results: list[ApplicationResult] = []
        import random
        for i, (_, post) in enumerate(queue[:limit]):
            # Count against the cap as we go, not only against history. The
            # pre-queue check reads a snapshot taken before the run, so without
            # this a single run could send every posting at one company.
            key = post.company.strip().lower()
            if key and applied_companies.get(key, 0) >= self.cfg.per_company_cap:
                log.info("run.company_cap_reached", company=post.company,
                         cap=self.cfg.per_company_cap)
                continue
            # Stay on one job until it is in, rather than filing a failure and
            # moving on. Most failures here are stateful, not permanent: a
            # Workday account that did not exist a minute ago exists now, a
            # menu that did not open will open, a heal round that ran out of
            # attempts gets another form to work on. Retrying immediately costs
            # a minute; coming back to it costs a whole pass.
            # In-tab retries live in _apply_with_retries. This outer loop only
            # re-leases when the tab itself died (browser crash, context
            # closed), which the in-tab loop cannot recover from.
            r = None
            for release in range(1, 4):
                r = await self.apply_to(post)
                if not r.reason.startswith("tab died") or not _worth_retrying(r):
                    break
                log.warning("run.releasing_tab", job_id=post.job_id,
                            company=post.company, attempt=release + 1, of=3)
                await asyncio.sleep(random.uniform(4.0, 9.0))
            results.append(r)
            if r.status in (Status.SUBMITTED.value, Status.CONFIRMED.value) and key:
                applied_companies[key] = applied_companies.get(key, 0) + 1
            if i < min(limit, len(queue)) - 1:
                await asyncio.sleep(random.uniform(*self.cfg.pace_seconds))
        return results
