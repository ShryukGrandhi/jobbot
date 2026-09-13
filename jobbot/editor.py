"""Profile editor: the form that fills `config/profile.yaml`.

Everything jobbot says in the candidate's name comes out of that file, and until
now the only way to write it was to hand-edit YAML. This is the same record as a
form, with room for the things no fixed schema anticipates.

Two rules the UI enforces, because they are the point of the project:

  * A screening answer is only ever what the user chose. Every one starts
    "not set" and stays that way until they pick. Nothing is pre-selected, no
    default is "yes", and "not set" is a real, saveable state that halts the
    relevant application rather than guessing.

  * Saving never silently destroys the previous answer set. The old file is
    copied to `config/profile.<timestamp>.yaml` before the new one is written,
    and the new one is validated through the pydantic model first -- an invalid
    submission leaves the file on disk untouched.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger(__name__)

# Every screening key the answering layer knows how to route a question to,
# with the shape of answer that key expects and why the form asks for it.
# `kind`: bool | text | choice. `legal` mirrors profile.LEGALLY_SIGNIFICANT.
SCREENING_SPEC: list[dict[str, Any]] = [
    {"key": "work_authorization", "kind": "bool", "legal": True,
     "label": "Are you legally authorized to work in the country you're applying in?",
     "help": "Asked on nearly every US application. A wrong answer here is a false statement, not a typo."},
    {"key": "requires_sponsorship_now", "kind": "bool", "legal": True,
     "label": "Do you now require visa sponsorship?",
     "help": "Answer for today. Some forms ask now and future separately, and they are not the same question."},
    {"key": "requires_sponsorship_future", "kind": "bool", "legal": True,
     "label": "Will you require sponsorship in the future?",
     "help": "F-1/OPT holders usually answer yes here even when 'now' is no."},
    {"key": "citizenship", "kind": "text", "legal": True,
     "label": "Citizenship", "placeholder": "United States",
     "help": "Only used when a form asks outright."},
    {"key": "visa_status", "kind": "text", "legal": True,
     "label": "Visa / work status",
     "placeholder": "US Citizen or Permanent Resident",
     "help": "Phrase it the way you would on a form. Free text; it gets matched to whatever options the form offers."},
    {"key": "criminal_history", "kind": "bool", "legal": True,
     "label": "Have you been convicted of a crime?",
     "help": "A widely-used open-source applier shipped a hardcoded answer to this for every user. That is why nothing here has a default."},
    {"key": "background_check_consent", "kind": "bool", "legal": True,
     "label": "Do you consent to a background check?"},
    {"key": "drug_test_consent", "kind": "bool", "legal": True,
     "label": "Do you consent to a drug test?"},
    {"key": "age_over_18", "kind": "bool", "legal": True,
     "label": "Are you over 18?"},
    {"key": "date_of_birth", "kind": "text", "legal": True,
     "label": "Date of birth", "placeholder": "leave empty unless you want it answered",
     "help": "Rarely required to apply. Leave empty and any form demanding it will stop for you instead."},
    {"key": "education_degree", "kind": "choice", "legal": True,
     "label": "Highest level of education",
     "options": ["High School", "Some College", "Associate's Degree",
                 "Bachelor's Degree", "Master's Degree", "MBA",
                 "Doctorate (PhD)", "Professional Degree (JD/MD)", "Other"],
     "help": "What you would claim today, not what you are working toward."},
    {"key": "education_completed", "kind": "bool", "legal": True,
     "label": "Have you completed that degree?",
     "help": "Answer no if you are still enrolled. The resume still shows an expected date."},
    {"key": "veteran_status", "kind": "choice", "legal": True,
     "label": "Protected veteran status",
     "options": ["I am not a protected veteran",
                 "I identify as one or more of the classifications of a protected veteran",
                 "I do not wish to answer"]},
    {"key": "disability_status", "kind": "choice", "legal": True,
     "label": "Disability status",
     "options": ["No, I do not have a disability",
                 "Yes, I have a disability, or have had one in the past",
                 "I do not wish to answer"]},
    {"key": "gender", "kind": "choice", "legal": False,
     "label": "Gender (EEO, voluntary)",
     "options": ["Male", "Female", "Non-binary", "I do not wish to answer"]},
    {"key": "ethnicity", "kind": "choice", "legal": False,
     "label": "Race / ethnicity (EEO, voluntary)",
     "options": ["Hispanic or Latino", "White", "Black or African American",
                 "Asian", "Native Hawaiian or Other Pacific Islander",
                 "American Indian or Alaska Native", "Two or More Races",
                 "I do not wish to answer"],
     "help": "'I do not wish to answer' is matched to whatever wording each form uses for declining, so the field is never left empty."},
    {"key": "previously_employed_here", "kind": "bool", "legal": True,
     "label": "Have you previously worked for this employer?",
     "help": "Answered per application. Set the answer that is true for most of them; a form where it differs will still ask you."},
    {"key": "previously_interviewed_here", "kind": "bool", "legal": True,
     "label": "Have you previously interviewed with this employer?"},
    {"key": "related_to_employee", "kind": "bool", "legal": True,
     "label": "Are you related to a current employee?"},
    {"key": "government_clearance", "kind": "bool", "legal": True,
     "label": "Do you hold a government security clearance?"},
    {"key": "export_control_us_person", "kind": "bool", "legal": True,
     "label": "Are you a U.S. person for export-control purposes? "
              "(U.S. citizen, lawful permanent resident, asylee or refugee. "
              "Asked by defence and aerospace employers under ITAR/EAR.)"},
    {"key": "non_compete", "kind": "bool", "legal": True,
     "label": "Are you bound by a non-compete or similar agreement?"},
    {"key": "professional_license", "kind": "bool", "legal": True,
     "label": "Do you hold a professional license relevant to this work?"},
    {"key": "arbitration_agreement", "kind": "choice", "legal": True,
     "label": "Will you agree to an arbitration clause when a form asks?",
     "options": ["Yes", "No"],
     "help": "This is a contract term. It is never inferred on your behalf -- leave it unset and any form asking will stop for you."},
    {"key": "policy_acknowledgement", "kind": "choice", "legal": True,
     "label": "Will you acknowledge employer policies (AI use, privacy notices)?",
     "options": ["Yes", "No"]},
]

WORK_PREF = ["", "remote", "hybrid", "onsite", "flexible"]

# Prompts for the free-text preference fields, so the box says what good input
# looks like instead of leaving the user to guess.
PREF_SPEC = [
    ("earliest_start", "Earliest start date", "text",
     "Two weeks from a signed offer",
     "Answered verbatim into 'when can you start'. A date works too."),
    ("notice_period_weeks", "Notice period (weeks)", "number", "2", ""),
    ("work_preference", "Preferred arrangement", "select", "", ""),
    ("how_heard", "How you heard about the role", "text", "Company website",
     "Used for the 'how did you hear about us' dropdown."),
    ("timeline_notes", "Timeline notes", "textarea",
     "Graduating in May, can start full-time after that; open to part-time before.",
     "Anything about availability that a date alone does not capture."),
    ("why_this_company_notes", "Why-this-company raw material", "textarea",
     "What genuinely interests you about the kind of company you're targeting, "
     "and which of your projects is the closest match to their work.",
     "NOT a canned paragraph. The model writes a per-company answer from this, "
     "so give it specifics it can pick from."),
]


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_PLACEHOLDERS = {"", "-", "--", "n/a", "na", "tbd", "unknown", "<unknown>",
                 "none", "null", "?", "???", "todo", "[unknown]"}


def _no_placeholder(v: Any) -> str:
    """Text, unless it is a stand-in for missing text.

    A model asked for a required field it cannot source will fill it with
    "<UNKNOWN>" rather than fail. That string would be typeset onto a resume
    and typed into an employer's form, so it is treated as absent.
    """
    s = str(v or "").strip()
    return "" if s.lower() in _PLACEHOLDERS else s


def first_number(v: Any) -> float | None:
    """The first number in a human-written value, or None.

    People write a GPA as "3.8/4.0", "3.8 out of 4.0", "GPA: 3.8" or "3.8" --
    all of them normal, none of them float()-able except the last. A bare
    float() raised ValueError straight out of the save, and because one
    exception fails the whole request, a single slash discarded every role and
    project in the submission.
    """
    text = str(v or "")
    # A decimal comma between single digits is a European decimal point, not a
    # thousands separator: "3,8" is 3.8, and reading it as 3.0 would put a
    # wrong GPA on a resume rather than no GPA.
    text = re.sub(r"(?<=\d),(?=\d\b)", ".", text)
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def _url(s: str) -> str:
    """Add a scheme to a bare domain.

    Dictation and resumes both produce "github.com/you/thing". A form field
    expecting a URL, and an <a href> on the resume, both need the scheme.
    """
    s = (s or "").strip()
    if not s or "://" in s or s.startswith("mailto:"):
        return s
    return "https://" + s.lstrip("/")


def load_raw(path: Path) -> dict[str, Any]:
    """The YAML as-is, so the form shows what is on disk rather than defaults."""
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("editor.unreadable_profile", path=str(path), error=str(exc)[:200])
        return {}


def to_form(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise on-disk YAML into the flat shape the browser form expects.

    Screening is the interesting part: on disk a value may be a bare scalar or
    a `{value, provenance}` dict, and `None`/absent must both come back as "not
    set" rather than as a falsy answer -- `false` and "unanswered" are different
    facts.
    """
    out = json.loads(json.dumps(raw, default=str))
    scr = out.get("screening") or {}
    flat: dict[str, Any] = {}
    for k, v in scr.items():
        if isinstance(v, dict):
            v = v.get("value")
        flat[k] = v
    out["screening"] = flat
    return out


def from_form(data: dict[str, Any]) -> dict[str, Any]:
    """Turn the browser payload into YAML-ready profile data.

    Empty strings are dropped rather than written, so a blank box means "not
    answered" instead of "answered with nothing" -- which for a screening key
    is the difference between the run stopping to ask and the run submitting a
    blank.
    """
    def clean_list(v: Any) -> list[str]:
        if isinstance(v, str):
            v = [x.strip() for x in v.replace("\n", ",").split(",")]
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    out: dict[str, Any] = {}
    ident = dict(data.get("identity") or {})
    loc = {k: str(v or "").strip() for k, v in (ident.pop("location", None) or {}).items()}
    out["identity"] = {
        **{k: (_url(str(v or "")) if k in ("linkedin", "github", "website")
               else str(v or "").strip())
           for k, v in ident.items()},
        "location": loc,
    }
    # An unset email must stay None. Coercing it to "" makes it a string that
    # EmailStr then rejects ("must have an @-sign"), which failed the save for
    # the exact case the optional field exists to allow.
    if not out["identity"].get("email"):
        out["identity"]["email"] = None

    for k in ("headline", "summary", "earliest_start", "work_preference",
              "timeline_notes", "how_heard", "why_this_company_notes"):
        out[k] = str(data.get(k) or "").strip()

    for k in ("target_titles", "target_locations", "target_companies",
              "exclude_companies", "awards",
              "publications", "certifications", "languages"):
        out[k] = clean_list(data.get(k))

    # Only write a boolean the caller actually sent. bool(data.get(k)) turned an
    # ABSENT key into False, so a profile built by intake -- whose payload has no
    # such keys -- claimed the candidate was open to no arrangement at all:
    # remote False, onsite False, hybrid False. That is not a default, it is a
    # false statement, and it drove a wrong answer to "are you open to working
    # in person?" on a real form. Absent now leaves the model default standing.
    for k in ("remote_ok", "onsite_ok", "hybrid_ok", "willing_to_relocate"):
        if k in data and data[k] is not None:
            out[k] = bool(data[k])

    try:
        out["min_requirement_match"] = float(data.get("min_requirement_match") or 0.5)
    except (TypeError, ValueError):
        out["min_requirement_match"] = 0.5

    # "2", "2 weeks", "two weeks" -> 2, 2, None. Same reason as the GPA: an
    # int(float(...)) here took the whole save down with it.
    npw = first_number(data.get("notice_period_weeks"))
    out["notice_period_weeks"] = int(npw) if npw is not None else None

    exp = []
    for e in data.get("experience") or []:
        # Company is the minimum that makes a role a role. Requiring a title too
        # meant a role whose title no source stated was silently dropped -- the
        # work vanished with no message.
        company = _no_placeholder(e.get("company"))
        if not company:
            continue
        exp.append({
            "company": company, "title": _no_placeholder(e.get("title")),
            "start": str(e.get("start") or "").strip() or None,
            "end": str(e.get("end") or "").strip() or None,
            "location": str(e.get("location") or "").strip(),
            "tech": clean_list(e.get("tech")),
            "bullets": [b.strip() for b in (e.get("bullets") or []) if str(b).strip()],
            "gap_explanation": str(e.get("gap_explanation") or "").strip() or None,
        })
    out["experience"] = exp

    edu = []
    for x in data.get("education") or []:
        if not str(x.get("school") or "").strip():
            continue
        edu.append({
            "school": x["school"].strip(),
            "degree": str(x.get("degree") or "").strip(),
            "field_of_study": str(x.get("field_of_study") or "").strip(),
            "start": str(x.get("start") or "").strip() or None,
            "end": str(x.get("end") or "").strip() or None,
            "gpa": first_number(x.get("gpa")),
            "completed": bool(x.get("completed")),
        })
    out["education"] = edu

    projects = []
    for p in data.get("projects") or []:
        if not str(p.get("name") or "").strip():
            continue
        # The resume renders `url`; a project that only has `repo` would print
        # with no link at all, which is the one thing a reviewer wants to click.
        url = str(p.get("url") or "").strip()
        repo = str(p.get("repo") or "").strip()
        projects.append({
            "name": p["name"].strip(),
            "description": str(p.get("description") or "").strip(),
            "url": _url(url or repo) or None,
            "repo": _url(repo) or None,
            "tech": clean_list(p.get("tech")),
            "bullets": [b.strip() for b in (p.get("bullets") or []) if str(b).strip()],
        })
    out["projects"] = projects

    # Two callers, two shapes: the browser form sends rows of
    # {category, items}, the intake merge sends the stored {category: [...]}
    # map. Accept both here rather than making each caller convert -- this is
    # the only validated way into the file, so it is the right place to be
    # tolerant.
    skills: dict[str, list[str]] = {}
    raw_skills = data.get("skills") or []
    rows = ([{"category": k, "items": v} for k, v in raw_skills.items()]
            if isinstance(raw_skills, dict) else raw_skills)
    for row in rows:
        if not isinstance(row, dict):
            continue
        cat = str(row.get("category") or "").strip()
        items = clean_list(row.get("items"))
        if cat and items:
            skills[cat] = items
    out["skills"] = skills

    comp = data.get("compensation") or {}
    def money(v: Any) -> int | None:
        """Salary as people write it: "$165,000", "165k", "165K", "competitive"."""
        raw = str(v or "").replace(",", "").replace("$", "").strip()
        n = first_number(raw)
        if n is None:
            return None
        if re.search(r"\d\s*[kK]\b", raw):
            n *= 1_000
        elif re.search(r"\d\s*[mM]\b", raw):
            n *= 1_000_000
        return int(n)
    out["compensation"] = {
        "target_base": money(comp.get("target_base")),
        "minimum_base": money(comp.get("minimum_base")),
        "currency": str(comp.get("currency") or "USD").strip() or "USD",
    }

    extra: dict[str, str] = {}
    raw_extra = data.get("extra") or []
    erows = ([{"label": k, "value": v} for k, v in raw_extra.items()]
             if isinstance(raw_extra, dict) else raw_extra)
    for row in erows:
        if not isinstance(row, dict):
            continue
        k = str(row.get("label") or "").strip()
        v = str(row.get("value") or "").strip()
        if k and v:
            extra[k] = v
    out["extra"] = extra

    # Screening. "" / None means the user has not answered, and an unanswered
    # key must be ABSENT -- writing it as empty would make `can_answer` false
    # anyway, but an absent key is also what the file's own comments promise.
    scr: dict[str, Any] = {}
    for k, v in (data.get("screening") or {}).items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        scr[str(k)] = v
    out["screening"] = scr
    return out


def validate(data: dict[str, Any]) -> tuple[Any | None, list[str]]:
    """Run the payload through the real model. Returns (profile, errors)."""
    from jobbot.profile import Profile

    try:
        return Profile.model_validate(data), []
    except Exception as exc:  # noqa: BLE001
        errs: list[str] = []
        for err in getattr(exc, "errors", lambda: [])():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            errs.append(f"{loc or 'profile'}: {err.get('msg', '')}")
        return None, errs or [str(exc)[:300]]


def save(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Validate, back up, then write. Returns a result dict for the UI."""
    profile, errors = validate(data)
    if profile is None:
        return {"ok": False, "errors": errors}

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.stem}.{_now_stamp()}{path.suffix}")
        shutil.copy2(path, backup)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(profile.model_dump(mode="json"),
                                  sort_keys=False, width=100, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)

    missing = profile.missing_legally_significant()
    # Saving a partial draft is allowed now, so the save has to say what is
    # still missing -- otherwise an incomplete profile looks finished right up
    # until a run refuses to start.
    incomplete = profile.missing_identity()
    undated = [f"{e.title or 'role'} at {e.company}" for e in profile.experience
               if e.start is None]
    untitled = [e.company for e in profile.experience if not e.title.strip()]
    log.info("editor.saved", path=str(path), backup=str(backup) if backup else None,
             missing=len(missing), incomplete=len(incomplete),
             undated=len(undated), untitled=len(untitled))
    return {
        "ok": True,
        "path": str(path),
        "backup": str(backup) if backup else None,
        "missing_core": missing,
        "missing_identity": incomplete,
        "undated_roles": undated,
        "untitled_roles": untitled,
        "roles": len(profile.experience),
        "years": profile.total_years_experience,
        "skills": sum(len(v) for v in profile.skills.values()),
        "screening_set": len(profile.screening),
    }


def resume_text(pdf_bytes: bytes) -> str:
    """Pull the text layer out of an uploaded resume, to type the profile from.

    Deliberately not an auto-filler. Parsing a resume into structured claims is
    exactly the step where a plausible-looking wrong employer or date gets
    introduced, and this file is the thing that is supposed to be true. The text
    is shown; the user copies what they recognise.
    """
    import io
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages).strip()
