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
import json
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from jobbot.ats import credentials as vault
from jobbot.ats import workday as wd
from jobbot.ats.detect import ATS, REQUIRES_ACCOUNT, detect
from jobbot.browser import capture as cap
from jobbot.browser.session import BrowserSession
from jobbot.discovery.sources import JobPost, ghost_score
from jobbot.forms.fill import apply_answer
from jobbot.forms.model import AnswerSource, FieldKind, ParsedForm, ProposedAnswer
from jobbot.healer import checkpoints as ck
from jobbot.healer.answer import deterministic_answers, model_answers
from jobbot.llm.client import LLMClient
from jobbot.notify import Notification, reliability_score
from jobbot.notify import send as notify_send
from jobbot.profile import Profile
from jobbot.resume.render import render_one_page
from jobbot.resume.tailor import fabrication_check, refine, sanitize_skills, tailor
from jobbot.tracker.answers_csv import AnswerLog
from jobbot.tracker.csv_tracker import Application, Status, Tracker

log = structlog.get_logger(__name__)


@dataclass
class RunConfig:
    data_dir: Path = Path("data")
    dry_run: bool = True              # fill everything, stop before submit
    make_github_project: bool = True
    publish_project_private: bool = False
    max_heal_rounds: int = 4
    max_resume_rounds: int = 5
    min_match_score: float = 0.5
    max_ghost_score: float = 0.6
    per_company_cap: int = 3
    gmail_enabled: bool = True
    notify: bool = True
    notify_to: str | None = None
    pace_seconds: tuple[float, float] = (25.0, 70.0)


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

    skills = [s.lower() for items in profile.skills.values() for s in items]
    hits = sum(1 for s in skills if s and s in text)
    skill_score = min(1.0, hits / max(6, len(skills) * 0.35)) if skills else 0.5

    titles = [t.lower() for t in profile.target_titles]
    title_score = 1.0 if any(t in post.title.lower() for t in titles) else 0.45
    if not titles:
        title_score = 0.6

    score = 0.6 * skill_score + 0.4 * title_score

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
        """Refuse to run autonomously with legally significant answers unset."""
        return self.profile.missing_legally_significant()

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
            async with self.session.tab(post.job_id) as page:
                return await self._apply_in_tab(page, post, audit, shots)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()[-1200:]
            log.error("apply.crashed", job_id=post.job_id, error=str(exc)[:200])
            (audit / "error.txt").write_text(tb)
            self.tracker.update(post.job_id, status=Status.FAILED.value,
                                error=str(exc)[:300])
            return ApplicationResult(post.job_id, Status.FAILED.value, str(exc)[:200])
        finally:
            await self.session.reap_orphans()

    async def _apply_in_tab(self, page: Any, post: JobPost, audit: Path,
                            shots: Path) -> ApplicationResult:
        jid = post.job_id
        await page.goto(post.url, wait_until="domcontentloaded")
        await cap.settle(page, quiet_ms=900)

        det = detect(page.url, None)
        log.info("apply.start", job_id=jid, ats=det.ats.value, company=post.company)

        # --- account wall (Workday and friends) --------------------------
        if det.ats in REQUIRES_ACCOUNT:
            if det.ats is ATS.WORKDAY:
                await wd.start_application(page)
                if await wd.needs_account(page):
                    res = await wd.ensure_account(
                        page, tenant=det.tenant or post.company,
                        email=str(self.profile.identity.email),
                        gmail_enabled=self.cfg.gmail_enabled)
                    if not res.signed_in:
                        self.tracker.update(jid, status=Status.UNREACHABLE.value,
                                            error=res.error[:250])
                        return ApplicationResult(jid, Status.UNREACHABLE.value,
                                                 f"account wall: {res.error}")
                    log.info("apply.account_ok", created=res.created,
                             emailed_code=res.needed_email_code)
            else:
                self.tracker.update(jid, status=Status.UNREACHABLE.value,
                                    error=f"{det.ats.value} requires an account; no adapter yet")
                return ApplicationResult(jid, Status.UNREACHABLE.value,
                                         f"{det.ats.value} account wall unsupported")

        # --- checkpoint 1: read the form ---------------------------------
        form, _ = await ck.checkpoint_parse(page, self.llm, shots)
        (audit / "form.json").write_text(json.dumps(
            {"submit": form.submit_label, "step": form.step,
             "fields": [f.to_prompt_dict() for f in form.fields]}, indent=2))

        if form.requires_account:
            self.tracker.update(jid, status=Status.UNREACHABLE.value,
                                error="form still gated behind sign-in")
            return ApplicationResult(jid, Status.UNREACHABLE.value, "sign-in gate")

        if not form.fields:
            self.tracker.update(jid, status=Status.FAILED.value,
                                error="no answerable fields found")
            return ApplicationResult(jid, Status.FAILED.value, "no fields")

        # --- knockout pre-scan, BEFORE any expensive work ----------------
        knockouts, skip = ck.knockout_scan(self.llm, self.profile, form, post.title)
        if skip and knockouts:
            reason = "; ".join(f"{k['label'][:40]}={k['honest_answer'][:20]}" for k in knockouts[:3])
            self.tracker.update(jid, status=Status.KNOCKOUT_FAIL.value,
                                knockout_reason=reason[:300],
                                questions_total=len(form.fields))
            log.info("apply.knockout_skip", job_id=jid, reason=reason[:120])
            return ApplicationResult(jid, Status.KNOCKOUT_FAIL.value, reason)

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
        (audit / "resume_critique.json").write_text(json.dumps(critique_history, indent=2))
        self._last_ats_score = float(
            next((h.get("final_ats_score", 0.0) for h in reversed(critique_history)
                  if "final_ats_score" in h), 0.0))

        dropped = sanitize_skills(self.profile, tailored)
        if dropped:
            (audit / "skills_removed.txt").write_text("\n".join(dropped))
        fabrications = fabrication_check(self.profile, tailored)
        if fabrications:
            # A resume that overstates is worse than no application.
            log.error("apply.fabrication_detected", job_id=jid, problems=fabrications[:4])
            (audit / "fabrication_report.txt").write_text("\n".join(fabrications))
            self.tracker.update(jid, status=Status.NEEDS_HUMAN.value,
                                error="resume fabrication check failed")
            return ApplicationResult(jid, Status.NEEDS_HUMAN.value,
                                     "fabrication check failed", flagged=fabrications)

        resume_pdf = audit / "resume.pdf"
        await render_one_page(page, tailored, resume_pdf)
        (audit / "resume_content.json").write_text(json.dumps(tailored, indent=2))
        self.tracker.update(jid, status=Status.PREPARED.value,
                            resume_path=str(resume_pdf), github_project_url=project_url,
                            match_score=fit_score(self.profile, post))

        # Re-navigate: rendering the PDF took this tab to a file:// URL.
        await page.goto(post.url, wait_until="domcontentloaded")
        await cap.settle(page, quiet_ms=800)
        if det.ats in REQUIRES_ACCOUNT and det.ats is ATS.WORKDAY:
            await wd.start_application(page)
        form, pc2 = await ck.checkpoint_parse(page, self.llm, shots)

        # --- answers ------------------------------------------------------
        det_answers, leftover = deterministic_answers(
            self.profile, form,
            published_salary=(post.salary_min, post.salary_max))
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
            (audit / "needs_human.txt").write_text("\n".join(reasons))
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
        for f in form.fields:
            if f.kind is FieldKind.FILE and not any(a.field_id == f.field_id for a in answers):
                answers.append(ProposedAnswer(
                    f.field_id, str(resume_pdf), AnswerSource.PROFILE, 1.0,
                    "tailored resume for this role"))

        by_id = {f.field_id: f for f in form.fields}
        filled = 0
        for a in answers:
            f = by_id.get(a.field_id)
            if f is None or not a.submittable:
                continue
            if await apply_answer(page, f, a, resume_path=resume_pdf):
                filled += 1
        log.info("apply.filled", job_id=jid, filled=filled, total=len(form.fields))

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
        ], indent=2, default=str))

        self.answer_log.record(
            job_id=jid, company=post.company, title=post.title,
            ats=post.ats.value, job_url=post.url,
            answers=json.loads((audit / "answers.json").read_text()))

        # --- checkpoint 2 + healing ---------------------------------------
        verification, rounds = await ck.heal(
            page, self.llm, self.profile, form, answers, shots,
            max_rounds=self.cfg.max_heal_rounds, resume_path=resume_pdf)
        (audit / "verification.json").write_text(json.dumps({
            "ready": verification.ready_to_submit,
            "summary": verification.summary,
            "issues": [i.__dict__ for i in verification.issues],
            "unfilled_required": verification.unfilled_required,
            "validation_errors": verification.validation_errors,
            "heal_rounds": rounds,
        }, indent=2))

        self.tracker.update(jid, questions_total=len(form.fields),
                            questions_answered=filled,
                            questions_flagged=len(blocked), heal_rounds=rounds)

        if not verification.ready_to_submit or verification.blockers:
            self.tracker.update(jid, status=Status.NEEDS_HUMAN.value,
                                error=f"{len(verification.blockers)} unresolved blockers")
            return ApplicationResult(jid, Status.NEEDS_HUMAN.value,
                                     "verification not clean", heal_rounds=rounds,
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
        submitted_click = False
        for sel in (f"button:has-text('{form.submit_label}')",
                    "[data-automation-id='bottom-navigation-submit-button']",
                    "button[type=submit]", "input[type=submit]"):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=8000)
                    submitted_click = True
                    break
            except Exception:  # noqa: BLE001
                continue
        await cap.settle(page, quiet_ms=2500)

        # --- checkpoint 3: one call, did it actually go through? ----------
        outcome, _ = await ck.checkpoint_outcome(page, self.llm, shots)
        self._record_lessons(post, outcome.lessons)

        if outcome.submitted:
            self.tracker.mark_submitted(jid, outcome.evidence)
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
            (audit / "project_rejected.txt").write_text("\n".join(problems))
            return None, ""

        local = audit / "project"
        pub = publish(ident, plan, private=self.cfg.publish_project_private,
                      author_name=self.profile.identity.full_name,
                      author_email=str(self.profile.identity.email),
                      keep_local=local, dry_run=True)

        smoke = smoke_test(pub.local_path, plan.get("run_command"))
        (audit / "project_smoke.json").write_text(json.dumps(smoke, indent=2))
        if not smoke["passed"]:
            log.warning("project.smoke_failed", output=smoke["output"][-200:])
            return None, ""

        import shutil
        shutil.rmtree(local, ignore_errors=True)
        pub = publish(ident, plan, private=self.cfg.publish_project_private,
                      author_name=self.profile.identity.full_name,
                      author_email=str(self.profile.identity.email),
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
        for p in posts:
            if self.tracker.already_applied(p.job_id):
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
                continue
            g = ghost_score(p, posts)
            if g > self.cfg.max_ghost_score:
                self.tracker.upsert(Application(
                    job_id=p.job_id, company=p.company, title=p.title, ats=p.ats.value,
                    job_url=p.url, status=Status.GHOST_SUSPECTED.value, ghost_score=str(g)))
                continue
            m = fit_score(self.profile, p)
            if m < self.cfg.min_match_score:
                self.tracker.upsert(Application(
                    job_id=p.job_id, company=p.company, title=p.title, ats=p.ats.value,
                    job_url=p.url, status=Status.FILTERED_OUT.value, match_score=str(m)))
                continue
            if applied_companies.get(p.company.lower(), 0) >= self.cfg.per_company_cap:
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
                 intern=by_tier.get(TIER_INTERN, 0), newgrad=by_tier.get(TIER_NEWGRAD, 0),
                 fulltime=by_tier.get(TIER_FULLTIME, 0), senior=by_tier.get(TIER_SENIOR, 0))

        results: list[ApplicationResult] = []
        import random
        for i, (_, post) in enumerate(queue[:limit]):
            results.append(await self.apply_to(post))
            if i < min(limit, len(queue)) - 1:
                await asyncio.sleep(random.uniform(*self.cfg.pace_seconds))
        return results
