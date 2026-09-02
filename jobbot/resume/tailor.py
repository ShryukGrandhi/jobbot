"""Tailor the master profile to one specific role.

What tailoring is: reordering, reselecting, and rephrasing real experience so
the parts that matter to THIS job are the parts a reader hits first.

What it is not: inventing experience. The model may not add a skill, a metric,
an employer, a date, or a responsibility that is not in the profile. Every
bullet it emits must be traceable to a bullet it was given.

Two findings shape the prompt. First, tailoring is worth roughly +30-50% on
callbacks -- real, and worth doing. Second, that signal is depreciating fast:
once AI made tailored prose free, its correlation with callbacks fell by about
half and with offers by ~79%, while employers reweighted onto verifiable work
history. The implication is not "stop tailoring", it is "tailoring must say
something specific and checkable, because generic fluency is now worthless."
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from jobbot.llm.client import LLMClient, cached_system
from jobbot.profile import Profile
from jobbot.style import STYLE_RULES

log = structlog.get_logger(__name__)

TAILOR_TOOL: dict[str, Any] = {
    "name": "tailored_resume",
    "description": "Return the resume content, tailored to this job, ready to typeset.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2 sentences max, concrete, no adjectives like 'passionate' or 'results-driven'. Omit entirely if it would only restate the experience section.",
            },
            "skills": {
                "type": "object",
                "description": "Category -> list of skills. Only skills present in the profile. Order most-relevant-first.",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
            "experience": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "title": {"type": "string"},
                        "dates": {"type": "string"},
                        "bullets": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Rewritten for relevance to this JD. Keep every number that was in the source bullet. Never introduce a number that was not.",
                        },
                    },
                    "required": ["company", "title", "dates", "bullets"],
                },
            },
            "projects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "bullets"],
                },
            },
            "awards": {
                "type": "array", "items": {"type": "string"},
                "description": "Up to 3 awards/publications from the profile, most relevant to THIS role first. Copy verbatim; never invent one.",
            },
            "keywords_used": {
                "type": "array", "items": {"type": "string"},
                "description": "Terms from the job description that legitimately appear in the candidate's real experience and are now surfaced.",
            },
            "keywords_missing": {
                "type": "array", "items": {"type": "string"},
                "description": "Requirements from the JD the candidate genuinely does NOT have. Report honestly; do not paper over them.",
            },
        },
        "required": ["skills", "experience"],
    },
}

TAILOR_SYSTEM = """You tailor one candidate's resume to one job. You are precise and honest.

HARD RULES:
- Every bullet you emit must correspond to a bullet in the source profile. You may
  rephrase, reorder, merge, shorten, or drop. You may NOT invent.
- Never add a metric, technology, employer, title, date, or responsibility that is
  not in the source. If a number is in the source, keep it exactly.
- Never claim familiarity with something the candidate has not touched. If the job
  wants Kafka and the candidate has none, that goes in keywords_missing, not into a bullet.
- Prefer the candidate's real numbers over adjectives. "Cut p99 from 840ms to 95ms"
  beats "significantly improved performance".
- Plain, declarative sentences. No "leveraged", "spearheaded", "passionate about".
- Surface job-description vocabulary ONLY where the candidate's real work already
  matches it. Aligning real experience to the reader's language is the goal;
  keyword stuffing is not, and no ATS scores you on keyword density anyway.

The resume must fit one page, so be ruthless about relevance: 3-5 bullets for recent
and relevant roles, 1-2 for older or less relevant ones.

""" + STYLE_RULES


def _coerce_skills(skills: Any) -> dict[str, list[str]]:
    """Normalize the skills block to {category: [skill, ...]}.

    Models intermittently return a comma-joined string for a category. Iterating
    that yields single characters, which then read as fabricated skills and halt
    the application. Split it instead.
    """
    out: dict[str, list[str]] = {}
    for cat, items in (skills or {}).items():
        if isinstance(items, str):
            out[cat] = [x.strip() for x in items.split(",") if x.strip()]
        elif isinstance(items, (list, tuple)):
            flat: list[str] = []
            for x in items:
                if isinstance(x, str):
                    flat.extend(y.strip() for y in x.split(",") if y.strip())
            out[cat] = flat
        else:
            out[cat] = []
    return out


def tailor(
    llm: LLMClient,
    profile: Profile,
    *,
    job_title: str,
    company: str,
    job_description: str,
    extra_project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce render-ready resume content for one job."""
    from jobbot.healer.answer import profile_digest

    src = profile_digest(profile)
    prompt = (
        f"TARGET ROLE: {job_title} at {company}\n\n"
        f"JOB DESCRIPTION:\n{job_description[:12000]}\n\n"
        "Tailor the resume. Return every role the candidate has, in reverse "
        "chronological order, with bullet counts weighted by relevance to this JD."
    )
    if extra_project:
        prompt += (
            "\n\nAdditionally, include this project, which was built specifically "
            "for this application and is real, working code:\n"
            + json.dumps(extra_project, indent=1)
        )

    r = llm.call(
        system=cached_system(TAILOR_SYSTEM + "\n\nCANDIDATE PROFILE:\n" + src),
        blocks=[{"type": "text", "text": prompt}],
        tool=TAILOR_TOOL,
        max_tokens=6000,
    )
    data = r.require_tool()
    data["skills"] = _coerce_skills(data.get("skills"))

    ident = profile.identity
    loc = ident.location
    data["identity"] = {
        "name": ident.full_name,
        "location": ", ".join(x for x in (loc.city, loc.state) if x),
        "email": str(ident.email),
        "phone": ident.phone,
        "linkedin": (ident.linkedin or "").replace("https://", "").replace("www.", ""),
        "github": (ident.github or "").replace("https://", "").replace("www.", ""),
        "website": (ident.website or "").replace("https://", "").replace("www.", ""),
    }
    if not data.get("awards"):
        data["awards"] = (profile.publications + profile.awards)[:3]
    data["education"] = [
        {"degree": e.degree + (f", {e.field_of_study}" if e.field_of_study else ""),
         "school": e.school,
         "dates": str(e.end.year) if e.end else ""}
        for e in profile.education
    ]

    log.info("resume.tailored", roles=len(data.get("experience", [])),
             used=len(data.get("keywords_used", [])),
             missing=len(data.get("keywords_missing", [])))
    return data


def fabrication_check(profile: Profile, tailored: dict[str, Any]) -> list[str]:
    """Flag anything in the output with no basis in the profile.

    A mechanical backstop to the prompt rules. It distinguishes two severities,
    because they are not the same kind of error:

    HALT -- an employer, title, or metric that is not in the profile. These are
    factual claims about the candidate's history. Getting one wrong is resume
    fraud, so the application stops.

    SANITIZE -- a skill label the profile does not list. Often a legitimate
    summary of real work ("computer vision" for someone who shipped Florence-2
    and SAM2 pipelines), sometimes an overreach. Dropping the label is both
    safer and less destructive than discarding an otherwise-correct
    application, so the skill is removed and the run continues.

    Matching is token-based. "Founders, Inc. (Pascal, Founders Inc Canopy)" is
    the same employer as "Founders, Inc." with the parenthetical moved, not a
    new one.
    """
    problems: list[str] = []

    def _sig(text: str) -> set[str]:
        words = re.findall(r"[a-z0-9+#.]+", (text or "").lower())
        out: set[str] = set()
        for w in words:
            w = w.strip(".")
            if len(w) < 2 or w in {"and", "the", "of", "for", "with", "a", "an", "inc", "llc"}:
                continue
            if len(w) > 3 and re.search(r"(s|x|z|ch|sh)es$", w):
                w = w[:-2]
            elif len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            out.add(w)
        return out

    def _known(candidate: str, reals: list[str]) -> bool:
        c = _sig(candidate)
        if not c:
            return True
        for r in reals:
            rs = _sig(r)
            if rs and (rs <= c or c <= rs):
                return True
        return False

    real_companies = [e.company for e in profile.experience]
    real_titles = [e.title for e in profile.experience]
    for e in tailored.get("experience", []):
        if not _known(e.get("company", ""), real_companies):
            problems.append(f"unknown employer in output: {e.get('company')!r}")
        if not _known(e.get("title", ""), real_titles):
            problems.append(f"unknown title in output: {e.get('title')!r}")

    source_nums: set[str] = set()
    for e in profile.experience:
        for b in e.bullets:
            source_nums.update(re.findall(r"\d[\d,.]*%?", b))
    for pr in profile.projects:
        for b in pr.bullets:
            source_nums.update(re.findall(r"\d[\d,.]*%?", b))
    for x in profile.publications + profile.awards:
        source_nums.update(re.findall(r"\d[\d,.]*%?", x))

    for e in tailored.get("experience", []):
        for b in e.get("bullets", []):
            for n in re.findall(r"\d[\d,.]*%?", b):
                if len(n) > 2 and n not in source_nums:
                    problems.append(f"number {n!r} not present in the source bullets: {b[:70]!r}")

    return problems


def sanitize_skills(profile: Profile, tailored: dict[str, Any]) -> list[str]:
    """Drop skill labels the profile does not support. Returns what was removed.

    Non-fatal by design: an unlisted skill is removed rather than allowed to
    discard an application whose experience section is entirely accurate.
    """
    real = {s.strip().lower() for items in profile.skills.values() for s in items}
    for e in profile.experience:
        real |= {t.strip().lower() for t in (e.tech or [])}
    for pr in profile.projects:
        real |= {t.strip().lower() for t in (pr.tech or [])}
    real.discard("")

    def _sig(text: str) -> set[str]:
        words = re.findall(r"[a-z0-9+#.]+", (text or "").lower())
        out: set[str] = set()
        for w in words:
            w = w.strip(".")
            if len(w) < 2 or w in {"and", "the", "of", "for", "with", "a", "an"}:
                continue
            if len(w) > 3 and re.search(r"(s|x|z|ch|sh)es$", w):
                w = w[:-2]
            elif len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            out.add(w)
        return out

    real_sets = [_sig(r) for r in real]
    removed: list[str] = []
    cleaned: dict[str, list[str]] = {}
    for cat, items in _coerce_skills(tailored.get("skills")).items():
        keep = []
        for sk in items:
            c = _sig(sk)
            if c and any(rs and (rs <= c or c <= rs) for rs in real_sets):
                keep.append(sk)
            else:
                removed.append(f"{sk} (under {cat})")
        if keep:
            cleaned[cat] = keep
    tailored["skills"] = cleaned
    if removed:
        log.info("resume.skills_sanitized", removed=removed[:6])
    return removed


CRITIQUE_TOOL: dict[str, Any] = {
    "name": "critique_resume",
    "description": (
        "Review a tailored resume against the job description as a hiring manager "
        "for THIS role would, and return concrete revisions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ship", "revise"]},
            "relevance_score": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "How well the top third of the page answers this JD.",
            },
            "strongest_signal": {"type": "string", "description": "The single best thing on the page for this role."},
            "weakest_bullets": {
                "type": "array", "items": {"type": "string"},
                "description": "Bullets that waste space for THIS role, verbatim.",
            },
            "revisions": {
                "type": "array",
                "description": "Concrete edits. Rephrase only; never introduce a fact.",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "original": {"type": "string"},
                        "replacement": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["company", "original", "replacement", "why"],
                },
            },
            "reorder_experience": {
                "type": "array", "items": {"type": "string"},
                "description": "Company names in the order they should appear, if the current order buries the most relevant role.",
            },
            "skills_to_surface": {
                "type": "array", "items": {"type": "string"},
                "description": "Skills already in the profile that should move earlier because the JD names them.",
            },
        },
        "required": ["verdict", "relevance_score", "revisions"],
    },
}

CRITIQUE_SYSTEM = """You are the hiring manager for the specific role below, reviewing this
resume in the ~30 seconds you actually give one.

Judge only: does the top third of this page make me want to read the rest, for THIS job?

Rules for your revisions:
- Rephrase, reorder, compress, cut. NEVER introduce a fact, metric, technology, or
  claim that is not already in the resume you were given.
- A replacement must be a strict rewrite of its original bullet.
- Prefer the candidate's real numbers over adjectives.
- If a bullet is genuinely irrelevant to this role, say so in weakest_bullets rather
  than inventing a way to make it relevant.
- Return verdict "ship" when further edits would be cosmetic. Do not manufacture work.
- Flag any bullet that reads as generic AI prose and rewrite it into the
  candidate's concrete specifics.

""" + STYLE_RULES


def refine(
    llm: LLMClient,
    profile: Profile,
    tailored: dict[str, Any],
    *,
    job_title: str,
    company: str,
    job_description: str,
    max_rounds: int = 5,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Critique-and-revise the tailored resume for this specific role.

    Bounded, and it stops as soon as the critic says ship. Every revision is
    re-checked against the profile, so a "improvement" that smuggles in a fact
    is discarded rather than printed.
    """
    from jobbot.resume.ats_score import score as ats_score

    history: list[dict[str, Any]] = []
    best = json.loads(json.dumps(tailored))
    best_score = ats_score(tailored, job_description,
                           profile_skills=profile.skills).overall

    stalled = 0
    for rnd in range(1, max_rounds + 1):
        cur = ats_score(tailored, job_description, profile_skills=profile.skills)
        targets = [
            f"Measured match score: {cur.overall:.0f}/100 "
            f"(keywords {cur.keyword_match:.0f}, skills {cur.skills_coverage:.0f}, "
            f"clarity {cur.experience_clarity:.0f}, impact {cur.impact_density:.0f}).",
        ]
        if cur.injectable:
            targets.append(
                "These are skills the candidate GENUINELY HAS (they are in the profile) "
                "that this job description asks for, but the current draft omits: "
                + ", ".join(cur.injectable[:10])
                + ". Surface them where the candidate's real work already involved them.")
        if cur.missing_keywords:
            targets.append(
                "Vocabulary from the job description absent from the resume: "
                + ", ".join(cur.missing_keywords[:10])
                + ". Use ONLY where the candidate's real work genuinely matches. "
                  "Do not insert a term the work does not support.")
        for rec in cur.recommendations[:4]:
            targets.append(rec)

        r = llm.call(
            system=cached_system(CRITIQUE_SYSTEM),
            blocks=[{"type": "text", "text":
                     f"ROLE: {job_title} at {company}\n\nJOB DESCRIPTION:\n"
                     f"{job_description[:9000]}\n\n"
                     "AUTOMATED ANALYSIS OF THE CURRENT DRAFT:\n- "
                     + "\n- ".join(targets)
                     + "\n\nCURRENT RESUME:\n"
                     f"{json.dumps({k: v for k, v in tailored.items() if k != 'identity'}, indent=1)}"}],
            tool=CRITIQUE_TOOL,
            max_tokens=5000,
        )
        c = r.require_tool()
        raw_score = c.get("relevance_score")
        try:
            score = float(raw_score)
            if score > 1.0:                     # models sometimes answer on 0-10
                score = round(min(score, 10.0) / 10.0, 3)
        except (TypeError, ValueError):
            score = None
        c["relevance_score"] = score
        history.append({
            "round": rnd,
            "verdict": c.get("verdict"),
            "relevance_score": c.get("relevance_score"),
            "strongest_signal": c.get("strongest_signal", ""),
            "revisions": len(c.get("revisions", [])),
        })
        log.info("resume.critique", round=rnd, verdict=c.get("verdict"),
                 score=c.get("relevance_score"), revisions=len(c.get("revisions", [])))

        if c.get("verdict") == "ship" or not c.get("revisions"):
            break

        applied = 0
        for rev in c.get("revisions", []):
            for e in tailored.get("experience", []):
                if e.get("company", "").strip().lower() != rev["company"].strip().lower():
                    continue
                for i, b in enumerate(e.get("bullets", [])):
                    if b.strip()[:60] == rev["original"].strip()[:60]:
                        e["bullets"][i] = rev["replacement"]
                        applied += 1
                        break

        order = [c_.strip().lower() for c_ in (c.get("reorder_experience") or [])]
        if order:
            tailored["experience"].sort(
                key=lambda e: order.index(e["company"].strip().lower())
                if e["company"].strip().lower() in order else 99)

        tailored["skills"] = _coerce_skills(tailored.get("skills"))
        surface = c.get("skills_to_surface") or []
        if surface and tailored.get("skills"):
            want = {s.strip().lower() for s in surface}
            for cat, items in tailored["skills"].items():
                tailored["skills"][cat] = (
                    [s for s in items if s.strip().lower() in want]
                    + [s for s in items if s.strip().lower() not in want])

        # A revision that smuggled in a new fact is worse than no revision.
        sanitize_skills(profile, tailored)
        problems = fabrication_check(profile, tailored)
        if problems:
            log.warning("resume.refine_rejected", round=rnd, problems=problems[:3])
            history[-1]["rejected_for_fabrication"] = problems[:3]
            break

        history[-1]["applied"] = applied

        # Keep a revision only if it measurably improved the match. A critique
        # round that scores worse is discarded rather than shipped.
        new_score = ats_score(tailored, job_description,
                              profile_skills=profile.skills).overall
        history[-1]["ats_score"] = new_score
        if new_score > best_score + 0.5:
            best, best_score = json.loads(json.dumps(tailored)), new_score
            stalled = 0
        elif new_score >= best_score:
            best, best_score = json.loads(json.dumps(tailored)), new_score
            stalled += 1
        else:
            log.info("resume.revision_regressed", round=rnd,
                     was=round(best_score, 1), now=round(new_score, 1))
            tailored = json.loads(json.dumps(best))
            break

        # Stop when the score stops climbing. Further rounds past a plateau are
        # the model rewording itself, which costs quota and risks drift.
        if stalled >= 2 or applied == 0:
            log.info("resume.plateau", round=rnd, score=round(best_score, 1))
            break

    final = ats_score(best, job_description, profile_skills=profile.skills)
    history.append({"final_ats_score": final.overall,
                    "keyword_match": final.keyword_match,
                    "skills_coverage": final.skills_coverage,
                    "injectable_missed": final.injectable[:8],
                    "recommendations": final.recommendations[:4]})
    log.info("resume.refined_final", score=final.overall, rounds=len(history) - 1)
    return best, history
