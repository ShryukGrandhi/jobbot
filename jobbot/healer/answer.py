"""Decide what to put in each field.

Two passes, deterministic first:

  1. Direct mapping. Name, email, phone, LinkedIn, GitHub, and every legally
     significant screening question come straight from the profile. No model is
     consulted, because there is nothing to decide -- and because a model asked
     for a fact it does not have will supply a plausible one. The canonical
     example from the wild: an agent that "just wrote 123 Main St" when it did
     not know an address.

  2. Model pass, for the remainder -- the essay questions, the role-specific
     ones, the odd phrasings. The model composes; it never asserts.

The guard that matters: a legally significant field is answerable ONLY from a
CONFIRMED profile value. If the profile does not have it, the field is flagged
for a human and the application stops. It is never inferred, never defaulted,
and never guessed -- not to "yes", not to the last option, not to anything.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from jobbot.forms.matching import match_boolean, match_numeric_range, match_option
from jobbot.forms.model import (
    AnswerSource, FieldKind, FormField, ParsedForm, ProposedAnswer,
)
from jobbot.llm.client import LLMClient, cached_system
from jobbot.llm.schemas import ANSWER_FIELDS_TOOL
from jobbot.profile import LEGALLY_SIGNIFICANT, Profile
from jobbot.style import STYLE_RULES

log = structlog.get_logger(__name__)


# Label patterns -> profile attribute. Ordered; first match wins.
_IDENTITY_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bfirst\s*name\b|\bgiven name\b", re.I), "first_name"),
    (re.compile(r"\blast\s*name\b|\bsurname\b|\bfamily name\b", re.I), "last_name"),
    (re.compile(r"\bpreferred name\b", re.I), "first_name"),
    (re.compile(r"\bfull name\b|^name$", re.I), "full_name"),
    (re.compile(r"\be-?mail\b", re.I), "email"),
    # Order matters: "Phone Country" must resolve to a country, not a phone
    # number. This is the classic wrong-field bug -- a positional or greedy
    # match writes the phone number into the country selector.
    (re.compile(r"phone\s*(country|code)|country\s*code|dial\s*code", re.I), "phone_country"),
    (re.compile(r"\bphone\b|\bmobile\b|\btelephone\b", re.I), "phone"),
    (re.compile(r"\blinked\s*in\b", re.I), "linkedin"),
    (re.compile(r"\bgithub\b", re.I), "github"),
    (re.compile(r"\b(personal )?(website|portfolio)\b", re.I), "website"),
    (re.compile(r"\bcity\b", re.I), "city"),
    (re.compile(r"\bstate\b|\bprovince\b", re.I), "state"),
    (re.compile(r"\bcountry\b", re.I), "country"),
    (re.compile(r"\b(zip|postal)\s*code\b", re.I), "postal_code"),
    (re.compile(r"\bstreet\b|\baddress line\b", re.I), "street"),
    (re.compile(r"address\b.{0,60}(plan|work|working|located|based)|where.{0,25}(work|working) from", re.I), "work_address"),
]

# Screening-question patterns -> profile.screening key.
_SCREENING_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"require.*(visa|employment).*sponsor|sponsorship.*(now|future)|need sponsorship", re.I), "requires_sponsorship_future"),
    (re.compile(r"\bsponsorship\b", re.I), "requires_sponsorship_now"),
    (re.compile(r"legally.*(authoriz|entitled).*work|work authoriz|authorized to work", re.I), "work_authorization"),
    (re.compile(r"\bcitizen(ship)?\b", re.I), "citizenship"),
    (re.compile(r"\bvisa status\b", re.I), "visa_status"),
    (re.compile(r"convicted|criminal|felony", re.I), "criminal_history"),
    (re.compile(r"background check", re.I), "background_check_consent"),
    (re.compile(r"drug (test|screen)", re.I), "drug_test_consent"),
    (re.compile(r"\bveteran\b|protected veteran", re.I), "veteran_status"),
    (re.compile(r"disability|disabled", re.I), "disability_status"),
    (re.compile(r"\bgender\b|\bsex\b", re.I), "gender"),
    (re.compile(r"\brace\b|ethnicit", re.I), "ethnicity"),
    (re.compile(r"\bhispanic\b|\blatino\b", re.I), "ethnicity"),
    (re.compile(r"(at least|over|are you).{0,12}18", re.I), "age_over_18"),
    (re.compile(r"security clearance|clearance", re.I), "government_clearance"),
    (re.compile(r"non-?compete", re.I), "non_compete"),
    (re.compile(r"(previously|ever).{0,30}(work|employ).{0,20}(here|for us|at)", re.I), "previously_employed_here"),
    (re.compile(r"(ever\s+)?interviewed?\b.{0,30}(here|before|with us|at)", re.I), "previously_interviewed_here"),
    (re.compile(r"arbitrat", re.I), "arbitration_agreement"),
    (re.compile(r"(ai|artificial intelligence)\s+policy|policy for application|acknowledge.{0,30}(policy|guidelines)", re.I), "policy_acknowledgement"),
    (re.compile(r"related to|family member.*employe", re.I), "related_to_employee"),
    (re.compile(r"professional licen[sc]e", re.I), "professional_license"),
    (re.compile(r"(highest )?(level of )?education|degree", re.I), "education_degree"),
]

_SALARY = re.compile(r"salary|compensation|pay (expectation|range)|desired (pay|comp)", re.I)
_YOE = re.compile(r"years? of (professional )?experience|how many years", re.I)


def _identity_value(profile: Profile, key: str) -> str:
    i = profile.identity
    return {
        "first_name": i.first_name, "last_name": i.last_name,
        "full_name": i.full_name, "email": str(i.email), "phone": i.phone,
        "linkedin": i.linkedin or "", "github": i.github or "",
        "website": i.website or "", "city": i.location.city,
        "state": i.location.state, "country": i.location.country,
        "postal_code": i.location.postal_code, "street": i.location.street,
        "phone_country": i.location.country,
        # Forms often ask where you will physically work from, which is the
        # candidate's location, not an unknown.
        "work_address": ", ".join(x for x in (i.location.city, i.location.state,
                                              i.location.country) if x),
    }.get(key, "")


def classify(field: FormField) -> None:
    """Tag a field with the profile key that answers it, in place."""
    label = field.label
    for pat, key in _SCREENING_MAP:
        if pat.search(label):
            field.profile_key = key
            field.legally_significant = key in LEGALLY_SIGNIFICANT
            return
    for pat, key in _IDENTITY_MAP:
        if pat.search(label):
            field.profile_key = f"identity.{key}"
            return
    if _SALARY.search(label):
        field.profile_key = "compensation"
    elif _YOE.search(label):
        field.profile_key = "years_experience"


def deterministic_answers(
    profile: Profile,
    form: ParsedForm,
    *,
    published_salary: tuple[int | None, int | None] = (None, None),
) -> tuple[list[ProposedAnswer], list[FormField]]:
    """Answer everything we can without a model. Returns (answers, leftovers)."""
    answers: list[ProposedAnswer] = []
    remaining: list[FormField] = []

    for f in form.fields:
        classify(f)
        key = f.profile_key

        if key and key.startswith("identity."):
            val = _identity_value(profile, key.split(".", 1)[1])
            if val:
                answers.append(ProposedAnswer(f.field_id, val, AnswerSource.PROFILE,
                                              1.0, "profile identity"))
                continue

        elif key == "compensation":
            s = profile.compensation.range_string(*published_salary)
            if s:
                # A numeric or dropdown salary field CAN auto-reject; a free-text
                # one cannot. Give the structured field a plain number.
                if f.kind in (FieldKind.NUMBER, FieldKind.SELECT):
                    n = profile.compensation.target_base
                    if n:
                        answers.append(ProposedAnswer(f.field_id, n, AnswerSource.PROFILE,
                                                      0.9, "target base"))
                        continue
                else:
                    answers.append(ProposedAnswer(f.field_id, s, AnswerSource.PROFILE,
                                                  0.9, "bolstering range"))
                    continue

        elif key == "years_experience":
            yrs = profile.total_years_experience
            if f.options:
                m, how = match_numeric_range(yrs, f.option_labels())
                if m:
                    answers.append(ProposedAnswer(f.field_id, m, AnswerSource.DERIVED,
                                                  0.85, f"{yrs}y -> {how}"))
                    continue
            else:
                answers.append(ProposedAnswer(f.field_id, int(yrs), AnswerSource.DERIVED,
                                              0.85, "sum of role durations"))
                continue

        elif key:
            # A screening question. The provenance guard lives here.
            ans = profile.answer(key)
            if not ans.usable_for(key):
                answers.append(ProposedAnswer(
                    f.field_id, None, AnswerSource.PROFILE, 0.0,
                    f"profile has no confirmed value for '{key}'",
                    needs_human=True,
                    blocked_reason=(
                        f"'{f.label[:70]}' is legally significant and unset in the profile"
                        if f.legally_significant else f"'{key}' unset in the profile"
                    ),
                ))
                continue

            v = ans.value
            # A yes/no combobox with no scraped options still needs mapping;
            # assume the conventional pair rather than typing "True".
            if isinstance(v, bool) and not f.options and f.kind in (
                    FieldKind.COMBOBOX, FieldKind.SELECT, FieldKind.RADIO):
                v = "Yes" if v else "No"
            if f.options:
                chosen = match_boolean(v, f.option_labels()) if isinstance(v, bool) else None
                if chosen is None:
                    chosen, _, _ = match_option(str(v), f.option_labels())
                if chosen is None:
                    answers.append(ProposedAnswer(
                        f.field_id, None, AnswerSource.PROFILE, 0.0,
                        "confirmed value does not map to any offered option",
                        needs_human=True,
                        blocked_reason=f"'{v}' matches none of {f.option_labels()[:6]}",
                    ))
                    continue
                v = chosen
            answers.append(ProposedAnswer(f.field_id, v, AnswerSource.PROFILE, 1.0,
                                          f"confirmed profile.screening.{key}"))
            continue

        remaining.append(f)

    return answers, remaining


PROFILE_SYSTEM = """You fill in job applications on behalf of one specific candidate.

CANDIDATE PROFILE (the only facts you may assert):
{profile}

{style}

Absolute rules:
- Never state a fact that is not in the profile above. If a field asks for
  something the profile does not contain, set needs_human=true.
- Never assert work authorization, visa sponsorship needs, citizenship, criminal
  history, consent to checks, veteran or disability status, age, or education
  completion. Those are handled elsewhere. Set needs_human=true if asked.
- For select/radio/combobox fields, return the option text VERBATIM.
- Write in the candidate's voice: specific, plain, no marketing language. Cite
  concrete things from the profile rather than adjectives.
- Respect any length limit given.
- Preference questions (remote/onsite willingness, start date, notice period,
  relocation, how you heard about us, why this company) ARE answerable from the
  PREFERENCES block. Use it rather than declining.
- Optional fields with nothing to say may be left null; that is not a failure.
- A REQUIRED field is different: answer it. Compose from the profile, the
  preferences block, and the page context. Only set needs_human on a required
  field if it is legally significant, or if answering would require inventing a
  fact. "I could phrase this better" is not a reason to decline.
- Essay and free-text prompts are the employer's own questions. Answer the
  question actually asked, in the candidate's voice, grounded in specifics from
  the profile. Respect any stated word or character count.
"""


def profile_digest(profile: Profile) -> str:
    """Compact profile rendering for the cached system prefix."""
    p = profile
    lines = [
        f"Name: {p.identity.full_name}",
        f"Location: {p.identity.location.city}, {p.identity.location.state}, {p.identity.location.country}",
        f"Headline: {p.headline}",
        f"Total professional experience: {p.total_years_experience} years",
        "", "EXPERIENCE:",
    ]
    for e in p.experience:
        end = e.end.isoformat() if e.end else "present"
        lines.append(f"- {e.title} at {e.company} ({e.start.isoformat()} to {end}), {e.location}")
        for b in e.bullets[:6]:
            lines.append(f"    * {b}")
        if e.tech:
            lines.append(f"    tech: {', '.join(e.tech)}")
    lines += ["", "EDUCATION:"]
    for ed in p.education:
        lines.append(f"- {ed.degree} in {ed.field_of_study}, {ed.school}"
                     + (f" ({ed.end.year})" if ed.end else ""))
    lines += ["", "SKILLS:"]
    for cat, items in p.skills.items():
        lines.append(f"- {cat}: {', '.join(items)}")
    if p.projects:
        lines += ["", "PROJECTS:"]
        for pr in p.projects:
            lines.append(f"- {pr.name}: {pr.description}" + (f" ({pr.url})" if pr.url else ""))
    if p.publications:
        lines += ["", "PUBLICATIONS:"]
        lines += [f"- {x}" for x in p.publications]
    if p.awards:
        lines += ["", "AWARDS & SELECTIVE PROGRAMS:"]
        lines += [f"- {x}" for x in p.awards]
    if p.summary:
        lines += ["", "SUMMARY:", p.summary]
    lines += ["", "PREFERENCES (usable for non-legal questions):", p.preferences_digest()]
    return "\n".join(lines)


def model_answers(
    llm: LLMClient,
    profile: Profile,
    fields: list[FormField],
    *,
    job_context: str = "",
    images: list[Any] | None = None,
    aria: str = "",
) -> list[ProposedAnswer]:
    """Answer every field the deterministic pass could not resolve.

    Every application invents its own questions -- "why us", essay prompts,
    role-specific screens, oddly-worded scales. A stored answer bank cannot
    cover them, so the model answers them here, from the rendered page rather
    than from label text alone. The screenshots carry help text, character
    limits, formatting hints and surrounding context that the label omits.
    """
    if not fields:
        return []

    payload = [f.to_prompt_dict() for f in fields]
    required = [f.label[:90] for f in fields if f.required]

    prompt_parts = []
    if job_context:
        prompt_parts.append(f"JOB CONTEXT:\n{job_context[:5000]}")
    if aria:
        prompt_parts.append("ACCESSIBILITY OUTLINE OF THE LIVE PAGE (for surrounding "
                            "context, help text and limits):\n" + aria[:12000])
    prompt_parts.append(
        "Answer EVERY field below. These are this employer's own questions -- they "
        "are not from a template, so answer each one on its own terms.\n\n"
        + "\n".join(f"{i+1}. {f}" for i, f in enumerate(payload)))
    if required:
        prompt_parts.append(
            "These are REQUIRED and must not be left blank unless they are legally "
            "significant (handled elsewhere) or the profile genuinely lacks the fact:\n- "
            + "\n- ".join(required))

    blocks: list[dict[str, Any]] = []
    if images:
        from jobbot.llm.client import encode_image
        for img in images[:8]:
            blocks.append(encode_image(img))
    blocks.append({"type": "text", "text": "\n\n".join(prompt_parts)})

    r = llm.call(
        system=cached_system(PROFILE_SYSTEM.format(profile=profile_digest(profile), style=STYLE_RULES)),
        blocks=blocks,
        tool=ANSWER_FIELDS_TOOL,
        max_tokens=8000,
    )
    data = r.require_tool()

    by_id = {f.field_id: f for f in fields}
    out: list[ProposedAnswer] = []
    for a in data.get("answers", []):
        fid = a.get("field_id", "")
        f = by_id.get(fid)
        if f is None:
            continue

        if a.get("needs_human") or a.get("value") in (None, ""):
            out.append(ProposedAnswer(fid, None, AnswerSource.COMPOSED, 0.0,
                                      a.get("rationale", ""), needs_human=True,
                                      blocked_reason="model could not answer from the profile"))
            continue

        # The model must never author a legally significant answer, whatever it
        # thinks. Belt and braces on top of the deterministic pass.
        if f.legally_significant or (f.profile_key in LEGALLY_SIGNIFICANT if f.profile_key else False):
            out.append(ProposedAnswer(fid, None, AnswerSource.COMPOSED, 0.0,
                                      "refused: legally significant", needs_human=True,
                                      blocked_reason=f"'{f.label[:70]}' must come from the profile"))
            continue

        v = a["value"]
        if f.options:
            chosen, score, _ = match_option(str(v), f.option_labels())
            if chosen is None and isinstance(v, bool):
                chosen = match_boolean(v, f.option_labels())
            if chosen is None:
                out.append(ProposedAnswer(fid, None, AnswerSource.COMPOSED, 0.0,
                                          "no option matched", needs_human=True,
                                          blocked_reason=f"'{str(v)[:40]}' matches no option"))
                continue
            v = chosen

        src = {"profile": AnswerSource.PROFILE, "derived": AnswerSource.DERIVED}.get(
            a.get("source", ""), AnswerSource.COMPOSED)
        out.append(ProposedAnswer(fid, v, src, float(a.get("confidence", 0.5)),
                                  a.get("rationale", "")))
    return out
