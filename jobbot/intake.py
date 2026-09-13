"""Turn an unstructured dump into a reviewable profile proposal.

The input is whatever the user has: a paragraph they dictated, several resumes
tailored for different roles, a LinkedIn URL, a GitHub URL, loose links. The
output is a *proposal* -- never a saved profile.

That distinction is the whole design. An LLM reading a resume will happily
produce a confident job title that is a paraphrase, a date that is a guess, or a
metric it rounded. Those become claims made under the user's name on a real
application. So the model proposes, the user accepts field by field against the
source text, and only then does anything reach `profile.yaml`.

Two hard constraints, enforced by the schema rather than by asking nicely:

  * There is no screening field in the tool schema. The model cannot propose an
    answer to work authorization, criminal history, sponsorship or consent even
    if the dump discusses them, because there is nowhere to put it. Those come
    only from the user choosing in the editor.

  * Numbers must be copied, not recomputed. A bullet's metric is the one piece
    of a resume a reader can check, and a "improved" figure is fabrication.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


# Note the absence of `screening`. That is load-bearing, not an oversight.
ORGANIZE_TOOL: dict[str, Any] = {
    "name": "organize_profile",
    "description": (
        "Extract everything the candidate has told you into structured profile "
        "fields. Report only what the source material actually states. Omit any "
        "field the sources do not support -- an omitted field is correct, an "
        "invented one is a false statement on a job application."
    ),
    "input_schema": _obj({
        "identity": _obj({
            "first_name": _STR, "last_name": _STR, "email": _STR, "phone": _STR,
            "linkedin": _STR, "github": _STR, "website": _STR,
            "city": _STR, "state": _STR, "country": _STR, "postal_code": _STR,
        }),
        "headline": {**_STR, "description": "One line, e.g. 'Software Engineer — backend and data infrastructure'. Only if the sources support it."},
        "summary": {**_STR, "description": "Two concrete sentences max, in the candidate's voice, built only from stated facts. Omit if it would only restate the experience section."},
        "experience": {
            "type": "array",
            "description": (
                "One entry per real role, NOT one per resume. Several tailored "
                "resumes describing the same job are the same job: merge them, "
                "keeping the most specific and most quantified version of each "
                "bullet. Put wording differences worth knowing about in notes."
            ),
            "items": _obj({
                "company": _STR, "title": _STR,
                "start": {**_STR, "description": "YYYY-MM-DD. Use the first of the month if only a month is given. Omit entirely if the sources do not say."},
                "end": {**_STR, "description": "YYYY-MM-DD, or empty string if this is the current role."},
                "location": _STR,
                "tech": _STRS,
                "bullets": {**_STRS, "description": "Copy the candidate's own numbers exactly. Never introduce a metric that is not in the source."},
                "source": {**_STR, "description": "Which input this came from, e.g. 'ml-resume.pdf' or 'dictation'."},
            }, ["company"]),
        },
        "education": {
            "type": "array",
            "items": _obj({
                "school": _STR, "degree": _STR, "field_of_study": _STR,
                "start": _STR, "end": _STR,
                "gpa": {**_STR, "description": "As written. Omit if not stated -- never estimate."},
                "completed": {"type": "boolean"},
                "source": _STR,
            }, ["school"]),
        },
        "projects": {
            "type": "array",
            "items": _obj({
                "name": _STR, "description": _STR, "url": _STR, "repo": _STR,
                "tech": _STRS, "bullets": _STRS, "source": _STR,
            }, ["name"]),
        },
        "skills": {
            "type": "array",
            "description": "Grouped. Only skills the sources actually claim.",
            "items": _obj({"category": _STR, "items": _STRS}, ["category", "items"]),
        },
        "awards": _STRS,
        "publications": _STRS,
        "certifications": _STRS,
        "languages": {**_STRS, "description": "Spoken languages only, not programming languages."},
        "target_titles": _STRS,
        "target_locations": _STRS,
        "target_companies": _STRS,
        "work_arrangement": _obj({
            "remote_ok": {"type": "boolean"},
            "onsite_ok": {"type": "boolean"},
            "hybrid_ok": {"type": "boolean"},
            "willing_to_relocate": {"type": "boolean"},
        }, []),
        "compensation": _obj({
            "target_base": _STR, "minimum_base": _STR, "currency": _STR,
        }),
        "preferences": _obj({
            "earliest_start": _STR,
            "notice_period_weeks": _STR,
            "work_preference": {**_STR, "description": "remote | hybrid | onsite | flexible"},
            "timeline_notes": _STR,
            "how_heard": _STR,
            "why_this_company_notes": {**_STR, "description": "Raw material for 'why us' answers: what genuinely interests them and which of their projects is closest. Not a canned paragraph."},
        }),
        "extra": {
            "type": "array",
            "description": (
                "Anything real and useful that no field above holds: open-source "
                "maintainership, talks, teaching, hackathon wins, clearances, "
                "preferred name, pronouns, availability quirks, accommodations, "
                "a short answer to 'tell me about yourself'. Label plus value."
            ),
            "items": _obj({"label": _STR, "value": _STR}, ["label", "value"]),
        },
        "notes": {
            "type": "array",
            "description": (
                "Things the user should know: contradictions between two resumes, "
                "a date you had to interpret, a claim too vague to place, anything "
                "you deliberately left out."
            ),
            "items": _obj({
                "observation": _STR,
                "severity": {"type": "string", "enum": ["conflict", "guess", "dropped", "info"]},
            }, ["observation", "severity"]),
        },
        "questions": {
            "type": "array",
            "description": (
                "Up to 8 questions whose answers would most improve the profile, "
                "most valuable first. Ask about missing facts, not preferences you "
                "could infer. Never ask a screening question -- work authorization, "
                "sponsorship, criminal history, consent, veteran or disability "
                "status are set by the user in the editor, not here."
            ),
            "items": _obj({
                "question": _STR,
                "why": {**_STR, "description": "One short clause on what it unlocks."},
                "field": {**_STR, "description": "Which profile area it fills."},
            }, ["question", "why"]),
        },
    }),
}

SYSTEM = """You organize a job applicant's raw material into a structured profile.

Everything you output becomes a claim the candidate makes to real employers under
their own name, so accuracy beats completeness every time.

RULES
- Report only what the sources state. If a field is not supported, omit it. An
  omitted field is correct; an invented one is a false statement on a job
  application.
- Copy numbers exactly. "Cut p99 from 840ms to 95ms" stays those numbers. Never
  round, scale, recompute, or add a metric that is not in the source.
- Never invent an employer, title, date, school, degree, or technology.
- work_arrangement: set only what they actually said. "SF or remote" states
  remote_ok and onsite_ok; it says nothing about relocation. Omit the rest.
- Never write a placeholder like "<UNKNOWN>", "N/A" or "TBD" into a field. If
  you do not have the value, leave the field out entirely -- it gets flagged
  for the candidate. A placeholder gets printed on a resume.
- Dates: use YYYY-MM-DD, first of the month when only a month is given. If a
  date is genuinely absent, omit it rather than estimating, and say so in notes.
- Several resumes tailored for different roles describe the SAME career. Merge
  them per role, keeping the most specific and most quantified phrasing. Do not
  emit one experience entry per resume.
- Where two sources conflict on a fact, take the more specific one and record
  the conflict in notes. Never silently pick.
- Dictated input is speech: it will have filler, restarts and asides. Clean it
  into plain prose. Cleaning means removing filler and fixing grammar -- it does
  NOT mean adding detail, sharpening a claim, or inferring a number.
- Never assert work authorization, visa sponsorship, citizenship, criminal
  history, background-check or drug-test consent, veteran or disability status,
  age, or education completion. There is deliberately nowhere in your output to
  put those; the candidate sets them themselves.
- Write bullets and prose in the candidate's own voice: plain, specific, no
  marketing language, no "passionate about", no "leveraged".

You already know what is in their profile. Propose ADDITIONS and CORRECTIONS
from the new material; do not re-propose a field that is already correct."""


def _sources_block(dump: str, resumes: list[dict[str, str]],
                   links: dict[str, str], answers: list[dict[str, str]]) -> str:
    parts: list[str] = []
    if links:
        rendered = "\n".join(f"  {k}: {v}" for k, v in links.items() if str(v).strip())
        if rendered:
            parts.append("LINKS THE CANDIDATE GAVE:\n" + rendered)
    if dump.strip():
        parts.append(
            "WHAT THE CANDIDATE SAID (dictated or typed, may be unstructured):\n"
            + dump.strip()[:30000])
    for r in resumes:
        name = r.get("name") or "resume"
        label = r.get("label") or ""
        text = (r.get("text") or "").strip()
        if not text:
            continue
        head = f"RESUME — {name}" + (f" (candidate's label: {label})" if label else "")
        parts.append(f"{head}:\n{text[:24000]}")
    if answers:
        qa = "\n\n".join(f"Q: {a.get('question','')}\nA: {a.get('answer','')}"
                         for a in answers if str(a.get("answer", "")).strip())
        if qa:
            parts.append("FOLLOW-UP ANSWERS:\n" + qa)
    return "\n\n---\n\n".join(parts)


def organize(
    llm: Any,
    *,
    dump: str = "",
    resumes: list[dict[str, str]] | None = None,
    links: dict[str, str] | None = None,
    answers: list[dict[str, str]] | None = None,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a reviewable proposal. Raises if there is nothing to work from."""
    resumes = resumes or []
    links = {k: v for k, v in (links or {}).items() if str(v).strip()}
    answers = answers or []

    sources = _sources_block(dump, resumes, links, answers)
    if not sources.strip():
        raise ValueError("nothing to organize — paste something, add a link, or drop a resume")

    known = json.dumps(_summarise_current(current or {}), indent=1)[:6000]

    r = llm.call(
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        blocks=[{"type": "text", "text":
                 f"ALREADY IN THE PROFILE (do not re-propose what is already right):\n{known}"
                 f"\n\n=== NEW MATERIAL ===\n\n{sources}"
                 "\n\n=== TASK ===\nOrganize this. Omit anything the sources do "
                 "not support, and put what you had to interpret in notes."}],
        tool=ORGANIZE_TOOL,
        max_tokens=12000,
    )
    out = r.require_tool()
    log.info("intake.organized",
             roles=len(out.get("experience") or []),
             projects=len(out.get("projects") or []),
             notes=len(out.get("notes") or []),
             questions=len(out.get("questions") or []),
             in_tokens=r.input_tokens, out_tokens=r.output_tokens)
    out["_usage"] = {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                     "cache_read": r.cache_read_tokens}
    return out


def _summarise_current(cur: dict[str, Any]) -> dict[str, Any]:
    """A compact view of the saved profile, so the model proposes deltas.

    Sending the whole thing back wastes the context that the new material needs;
    the model only has to know what it should not repeat.
    """
    ident = cur.get("identity") or {}
    return {
        "name": f"{ident.get('first_name','')} {ident.get('last_name','')}".strip(),
        "email": ident.get("email", ""),
        "headline": cur.get("headline", ""),
        "has_summary": bool(cur.get("summary")),
        "roles": [f"{e.get('title','')} at {e.get('company','')}"
                  for e in (cur.get("experience") or [])],
        "schools": [e.get("school", "") for e in (cur.get("education") or [])],
        "projects": [p.get("name", "") for p in (cur.get("projects") or [])],
        "skill_categories": list((cur.get("skills") or {}).keys())
                            if isinstance(cur.get("skills"), dict) else [],
        "target_titles": cur.get("target_titles") or [],
        "extra_labels": list((cur.get("extra") or {}).keys())
                        if isinstance(cur.get("extra"), dict) else [],
    }


QUESTIONS_TOOL: dict[str, Any] = {
    "name": "ask_questions",
    "description": (
        "Ask the questions whose answers would most improve this profile, given "
        "what is already in it."
    ),
    "input_schema": _obj({
        "questions": {
            "type": "array",
            "items": _obj({
                "question": _STR,
                "why": {**_STR, "description": "One short clause on what it unlocks."},
                "field": _STR,
                "placeholder": {**_STR, "description": "An example of the shape of a good answer."},
            }, ["question", "why"]),
        },
    }, ["questions"]),
}

INTERVIEW_SYSTEM = """You are filling gaps in a job applicant's profile by asking them things.

Ask what an application will need and the profile cannot currently answer.
Prioritise, in order: quantified outcomes for roles whose bullets have no
numbers; missing dates; unexplained gaps over six months; projects with no link;
what they actually want next; anything a "tell us about a time" question would
need.

Ask ONE thing per question, concretely and in plain language. "What did the
ingestion rebuild actually change, in numbers?" not "Tell us about your
achievements."

Never ask about work authorization, visa sponsorship, citizenship, criminal
history, background-check or drug-test consent, veteran or disability status,
age, or education completion. Those are set by the candidate directly, and a
question here would put an answer in the wrong place."""


def interview_questions(llm: Any, *, current: dict[str, Any] | None = None,
                        dump: str = "", count: int = 8) -> list[dict[str, str]]:
    """Questions targeted at what this specific profile is missing."""
    known = json.dumps(_full_gaps(current or {}), indent=1)[:8000]
    r = llm.call(
        system=[{"type": "text", "text": INTERVIEW_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        blocks=[{"type": "text", "text":
                 f"THE PROFILE AS IT STANDS:\n{known}"
                 + (f"\n\nTHEY ALSO SAID:\n{dump[:8000]}" if dump.strip() else "")
                 + f"\n\nAsk up to {count} questions, most valuable first."}],
        tool=QUESTIONS_TOOL,
        max_tokens=2500,
    )
    qs = r.require_tool().get("questions", [])
    log.info("intake.questions", count=len(qs))
    return qs[:count]


def _full_gaps(cur: dict[str, Any]) -> dict[str, Any]:
    """What is present, and specifically what is thin -- the model needs both."""
    import re

    roles = []
    for e in cur.get("experience") or []:
        bullets = e.get("bullets") or []
        quantified = sum(1 for b in bullets if re.search(r"\d", str(b)))
        roles.append({
            "title": e.get("title", ""), "company": e.get("company", ""),
            "start": str(e.get("start") or ""), "end": str(e.get("end") or "present"),
            "bullets": len(bullets), "bullets_with_a_number": quantified,
            "has_tech": bool(e.get("tech")),
        })
    return {
        "headline": cur.get("headline", ""),
        "summary": cur.get("summary", ""),
        "roles": roles,
        "education": [{"school": e.get("school", ""), "gpa": e.get("gpa"),
                       "end": str(e.get("end") or "")}
                      for e in (cur.get("education") or [])],
        "projects": [{"name": p.get("name", ""), "has_link": bool(p.get("url")),
                      "bullets": len(p.get("bullets") or [])}
                     for p in (cur.get("projects") or [])],
        "skills": cur.get("skills") or {},
        "awards": cur.get("awards") or [],
        "publications": cur.get("publications") or [],
        "certifications": cur.get("certifications") or [],
        "target_titles": cur.get("target_titles") or [],
        "target_locations": cur.get("target_locations") or [],
        "preferences": {k: cur.get(k, "") for k in
                        ("earliest_start", "work_preference", "timeline_notes",
                         "how_heard", "why_this_company_notes")},
        "extra": cur.get("extra") or {},
    }


# identity.location fields, as the model reports them versus where they live.
LOCATION_KEYS = frozenset({"city", "state", "country", "postal_code", "street"})


def apply_patches(raw: dict[str, Any], patches: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge accepted card patches into the on-disk profile shape.

    Server-side on purpose: the browser holds the display shape (skills as rows)
    while the file holds the storage shape (skills as a map), and doing the merge
    in one place means the save path stays the single validated route in.

    Adds are deduped, because organizing twice from overlapping material is the
    normal case, not the exception.
    """
    out = json.loads(json.dumps(raw, default=str)) if raw else {}

    def same_role(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return (str(a.get("company", "")).strip().lower() == str(b.get("company", "")).strip().lower()
                and str(a.get("title", "")).strip().lower() == str(b.get("title", "")).strip().lower())

    for p in patches:
        for key, val in p.items():
            if key == "identity":
                # The extractor reports city/state/country/postal_code flat, but
                # the record nests them under identity.location. Writing them
                # flat meant pydantic dropped them on the floor: the card said
                # "CITY San Francisco", you accepted it, and nothing arrived.
                ident = out.setdefault("identity", {})
                loc = ident.setdefault("location", {})
                for k, v in val.items():
                    if not str(v).strip():
                        continue
                    (loc if k in LOCATION_KEYS else ident)[k] = v
            elif key == "experience_add":
                cur = out.setdefault("experience", [])
                hit = next((x for x in cur if same_role(x, val)), None)
                if hit is None:
                    cur.append(val)
                else:
                    # Same role from another resume: union the bullets rather
                    # than duplicating the job or dropping the new phrasing.
                    have = {str(b).strip().lower() for b in hit.get("bullets") or []}
                    hit.setdefault("bullets", []).extend(
                        b for b in val.get("bullets") or []
                        if str(b).strip().lower() not in have)
                    have_t = {str(t).strip().lower() for t in hit.get("tech") or []}
                    hit.setdefault("tech", []).extend(
                        t for t in val.get("tech") or []
                        if str(t).strip().lower() not in have_t)
            elif key == "education_add":
                cur = out.setdefault("education", [])
                if not any(str(x.get("school", "")).strip().lower()
                           == str(val.get("school", "")).strip().lower() for x in cur):
                    cur.append(val)
            elif key == "projects_add":
                cur = out.setdefault("projects", [])
                if not any(str(x.get("name", "")).strip().lower()
                           == str(val.get("name", "")).strip().lower() for x in cur):
                    cur.append(val)
            elif key == "skills_add":
                skills = out.setdefault("skills", {})
                if not isinstance(skills, dict):
                    skills = {}
                cat = str(val.get("category", "")).strip()
                if cat:
                    have = {str(s).strip().lower() for s in skills.get(cat, [])}
                    skills[cat] = list(skills.get(cat, [])) + [
                        s for s in val.get("items") or []
                        if str(s).strip().lower() not in have]
                    out["skills"] = skills
            elif key == "extra_add":
                extra = out.setdefault("extra", {})
                if not isinstance(extra, dict):
                    extra = {}
                lab = str(val.get("label", "")).strip()
                if lab:
                    extra[lab] = str(val.get("value", ""))
                    out["extra"] = extra
            elif key == "compensation":
                out.setdefault("compensation", {}).update(
                    {k: v for k, v in val.items() if str(v).strip()})
            elif key.endswith("_add"):
                base = key[:-4]
                have = {str(x).strip().lower() for x in out.get(base) or []}
                out[base] = list(out.get(base) or []) + [
                    x for x in val if str(x).strip().lower() not in have]
            else:
                out[key] = val

    # Screening is never touched by intake. Whatever was set stays set, and
    # nothing new appears -- the editor is the only way in.
    out["screening"] = raw.get("screening") or {}
    return out


def to_cards(proposal: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a proposal into reviewable cards, each independently acceptable.

    One card per decision the user actually has to make. A card carries the patch
    that applies it, so accepting is a merge and not a re-parse.
    """
    cards: list[dict[str, Any]] = []
    cur_ident = current.get("identity") or {}

    def add(kind: str, title: str, body: Any, patch: dict[str, Any],
            *, source: str = "", replaces: str = "") -> None:
        cards.append({"id": f"c{len(cards)}", "kind": kind, "title": title,
                      "body": body, "patch": patch, "source": source,
                      "replaces": replaces})

    ident = proposal.get("identity") or {}
    id_new = {k: v for k, v in ident.items()
              if str(v).strip() and str(cur_ident.get(k, "")).strip() != str(v).strip()}
    if id_new:
        add("identity", "Contact details", id_new, {"identity": id_new})

    for key, label in (("headline", "Headline"), ("summary", "Summary")):
        v = str(proposal.get(key) or "").strip()
        if v and v != str(current.get(key) or "").strip():
            add(key, label, v, {key: v}, replaces=str(current.get(key) or ""))

    for e in proposal.get("experience") or []:
        add("experience", f"{e.get('title','')} — {e.get('company','')}", e,
            {"experience_add": e}, source=e.get("source", ""))
    for e in proposal.get("education") or []:
        add("education", e.get("school", ""), e, {"education_add": e},
            source=e.get("source", ""))
    for p in proposal.get("projects") or []:
        add("project", p.get("name", ""), p, {"projects_add": p},
            source=p.get("source", ""))
    for s in proposal.get("skills") or []:
        add("skills", f"Skills — {s.get('category','')}", s.get("items") or [],
            {"skills_add": s})
    for x in proposal.get("extra") or []:
        add("extra", x.get("label", ""), x.get("value", ""), {"extra_add": x})

    for key, label in (("awards", "Awards"), ("publications", "Publications"),
                       ("certifications", "Certifications"),
                       ("languages", "Languages spoken"),
                       ("target_titles", "Target titles"),
                       ("target_locations", "Target locations"),
                       ("target_companies", "Target companies")):
        items = [str(x).strip() for x in (proposal.get(key) or []) if str(x).strip()]
        have = {str(x).strip().lower() for x in (current.get(key) or [])}
        fresh = [x for x in items if x.lower() not in have]
        if fresh:
            add("list", label, fresh, {f"{key}_add": fresh})

    arr = {k: v for k, v in (proposal.get("work_arrangement") or {}).items()
           if isinstance(v, bool)}
    if arr:
        add("arrangement", "Work arrangement", arr, arr)

    comp = {k: v for k, v in (proposal.get("compensation") or {}).items()
            if str(v).strip()}
    if comp:
        add("compensation", "Compensation", comp, {"compensation": comp})

    prefs = {k: v for k, v in (proposal.get("preferences") or {}).items()
             if str(v).strip() and str(current.get(k) or "").strip() != str(v).strip()}
    for k, v in prefs.items():
        add("preference", k.replace("_", " "), v, {k: v},
            replaces=str(current.get(k) or ""))

    return cards
