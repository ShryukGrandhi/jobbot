"""The three vision checkpoints, and the healing loop between them.

  1. PARSE   -- before touching anything. Read every question off the rendered
                page, cross-checked against the DOM. Catches fields the DOM
                misses (canvas widgets, iframed sections) and fields the DOM
                reports but a human cannot see.

  2. VERIFY  -- after filling, before submitting. Re-read the page as rendered
                and compare every visible value against the profile. Loop:
                fix, re-screenshot, re-verify, until clean or the round budget
                runs out. This is where a wrong answer gets caught while it is
                still retractable.

  3. OUTCOME -- exactly one call after submitting. Did it actually go through,
                and what should change next time?

Checkpoint 3 exists because silent success is the dominant failure mode in this
category of software. Prior art is full of it: a widely-used applier logs
"applied successfully" for jobs that were never submitted, with users reporting
their success file holding far more entries than their account shows
applications. A click is not confirmation. Only affirmative on-page evidence is.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import structlog

from jobbot.browser import capture as cap
from jobbot.forms.model import (
    AnswerSource,
    FieldKind, FormField, ParsedForm, ProposedAnswer,
    Verification, VerificationIssue,
)
from jobbot.forms.extract import extract_form
from jobbot.llm.client import LLMClient, cached_system
from jobbot.llm.schemas import (
    KNOCKOUT_TOOL, PARSE_FORM_TOOL, POST_SUBMIT_TOOL, VERIFY_FORM_TOOL,
)
from jobbot.forms.matching import is_decline, match_decline, match_option
from jobbot.profile import LEGALLY_SIGNIFICANT, Profile


log = structlog.get_logger(__name__)

READER_SYSTEM = (
    "You read job application forms with care. You report only what is actually "
    "visible in the screenshots provided. You never invent a field, a value, or "
    "a confirmation message."
)


@dataclass
class Outcome:
    submitted: bool = False
    evidence: str = ""
    still_on_form: bool = True
    errors: list[str] = dc_field(default_factory=list)
    answers_correct: bool = True
    lessons: list[dict[str, str]] = dc_field(default_factory=list)


# Two kinds of marker. Anywhere in the string: things no real answer
# contains. Only at the START: possessives and adjectives that open a
# description ("the candidate's phone", "your real address") but also occur
# mid-sentence in every honest essay ("core to your stack") -- matching those
# anywhere rejected a 1,250-character cover-letter answer as a placeholder.
_DESCRIPTION_MARKERS = re.compile(
    r"\bplaceholder\b|\bTBD\b|\bN/?A\b|<[^>]+>|\[[^\]]+\]|"
    r"\bshould be\b|\bmust be\b|\bneeds to be\b|\benter (a|an|the|your)\b|"
    r"^\s*(the |a |an )?(candidate'?s?|user'?s?|applicant'?s?|real|actual|valid|their|your)\b",
    re.I,
)


def _looks_like_a_description(v: str) -> bool:
    """True when the model described a value instead of supplying one."""
    v = (v or "").strip()
    if not v:
        return True
    return bool(_DESCRIPTION_MARKERS.search(v))


# A label that is really just the control's own prompt to the user.
_IS_PLACEHOLDER = re.compile(
    r"^\s*(start typing|type here|select\b|pick |choose |search\b|enter )", re.I)


def _merge(dom: ParsedForm, seen: dict[str, Any]) -> ParsedForm:
    """Reconcile the vision reading with the DOM reading.

    The DOM owns selectors and option values. Vision owns visibility, required
    markers, and legal significance. Where vision reports a field the DOM did
    not surface, keep it -- flagged, since we have no selector for it yet.
    """
    # Match on the label with its required-marker and punctuation removed. The
    # DOM reads "Resume" and vision reads "Resume*"; comparing those raw meant
    # neither matched, so one Ashby form produced seventeen fields for nine
    # controls -- every one filled twice, and the resume attached to the
    # optional "Resume / CV" slot while the required "Resume*" stayed empty.
    def _key(text: str) -> str:
        return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).strip()[:60]

    by_label = {_key(f.label): f for f in dom.fields}
    claimed: set[int] = set()

    for vf in seen.get("fields", []):
        key = _key(vf.get("label") or "")
        match = by_label.get(key)
        if match is None and key:
            squashed = re.sub(r"\s+", "", key)
            for lab, f in by_label.items():
                if not lab:
                    continue
                if re.sub(r"\s+", "", lab) == squashed:
                    match = f
                    break
                if (lab in key or key in lab) and min(len(lab), len(key)) > 12:
                    match = f
                    break

        if match is None and key:
            # Labels can disagree completely: the DOM reads a date picker's
            # placeholder, "Pick date...", while vision reads the question
            # above it, "Available Start Date*". If exactly one unclaimed DOM
            # field has the same distinctive kind, it is that field -- and
            # keeping them apart left the answer on the vision copy, which has
            # no selector and so can never be filled.
            # A control whose only label is its own placeholder -- "Start
            # typing...", "Select...", "Pick date..." -- tells the model
            # nothing about what is being asked. Dedalus Labs' "which
            # programming languages" field reads as "Start typing...", so it
            # was answered blind and the submit was refused for a missing
            # required field. Vision reads the question above it; take that.
            same_kind = [f for f in dom.fields
                         if f.kind.value == (vf.get("kind") or "")
                         and id(f) not in claimed]
            placeholders = [f for f in same_kind if _IS_PLACEHOLDER.match(f.label or "")]
            if len(placeholders) == 1 and (vf.get("label") or ""):
                match = placeholders[0]
                match.label = vf["label"][:200]
                log.debug("merge.label_from_vision", label=match.label[:50])

            if match is None and (vf.get("kind") or "") in {
                    "date", "file", "textarea", "phone", "email"}:
                same = [f for f in dom.fields
                        if f.kind.value == vf["kind"] and id(f) not in claimed]
                if len(same) == 1:
                    match = same[0]
                    # The question reads better than a placeholder.
                    if len(vf.get("label") or "") > len(match.label):
                        match.label = vf["label"][:200]
        if match is not None:
            claimed.add(id(match))
            match.required = match.required or bool(vf.get("required"))
            match.legally_significant = (match.legally_significant
                                         or bool(vf.get("legally_significant")))
            if vf.get("help_text") and not match.help_text:
                match.help_text = vf["help_text"][:400]
            if not match.options and vf.get("options"):
                from jobbot.forms.model import FieldOption
                match.options = [FieldOption(label=o) for o in vf["options"]]
        else:
            from jobbot.forms.model import FieldOption
            dom.fields.append(FormField(
                field_id=vf.get("field_id") or f"vision_{len(dom.fields)}",
                label=vf.get("label", ""),
                kind=FieldKind(vf["kind"]) if vf.get("kind") in
                     {k.value for k in FieldKind} else FieldKind.UNKNOWN,
                required=bool(vf.get("required")),
                options=[FieldOption(label=o) for o in vf.get("options", [])],
                help_text=(vf.get("help_text") or "")[:400],
                group=(vf.get("section") or "")[:120],
                legally_significant=bool(vf.get("legally_significant")),
                selector="",   # no handle: fill will have to locate it by label
            ))

    if seen.get("submit_label"):
        dom.submit_label = seen["submit_label"]
    dom.page_title = seen.get("page_title") or dom.page_title
    dom.step = seen.get("step", "") or ""
    dom.total_steps = seen.get("total_steps")
    dom.requires_account = bool(seen.get("requires_account"))
    dom.notes = seen.get("notes", "") or ""
    return dom


# -- checkpoint 1 ---------------------------------------------------------

async def checkpoint_parse(
    page: Any, llm: LLMClient, shots_dir: str | Path,
) -> tuple[ParsedForm, cap.PageCapture]:
    """Read the whole form before touching it."""
    shots_dir = Path(shots_dir)
    dom = await extract_form(page)
    pc = await cap.capture(page, shots_dir, prefix="cp1")

    r = llm.vision(
        system=cached_system(READER_SYSTEM),
        prompt=(
            "These screenshots are consecutive vertical slices of ONE application "
            "page, top to bottom, with slight overlap; do not double-count a field "
            "that spans two slices.\n\n"
            "Accessibility outline of the same page (authoritative for what is "
            "answerable):\n" + (pc.aria[:20000] or "(unavailable)") + "\n\n"
            "Report every answerable field. Set legally_significant=true for any "
            "question asserting work authorization, visa sponsorship, citizenship, "
            "criminal history, background-check or drug-test consent, veteran or "
            "disability status, age, education completion, or licensure."
        ),
        images=pc.tiles,
        tool=PARSE_FORM_TOOL,
        max_tokens=8000,
    )
    merged = _merge(dom, r.require_tool())
    log.info("checkpoint1.parsed", dom_fields=len(dom.fields),
             total=len(merged.fields), required=len(merged.required_fields()),
             requires_account=merged.requires_account, tiles=pc.tile_count)
    return merged, pc


# -- knockout pre-scan ----------------------------------------------------

def knockout_scan(
    llm: LLMClient, profile: Profile, form: ParsedForm, job_title: str = "",
) -> tuple[list[dict[str, str]], bool]:
    """Would an honest answer here get us auto-rejected?

    Worth doing before any expensive work. Only structured questions (yes/no,
    single- and multi-select) can drive an auto-reject rule; free text cannot.
    And an auto-rejected application is suppressed from reviewer notifications
    entirely -- nobody is told, so it fails completely silently.

    Running this first is also what avoids the waste pattern seen in the field:
    one published run generated 2,019 tailored resumes to make 112 submissions,
    because it tailored before checking the form was even winnable.
    """
    structured = [f for f in form.fields
                  if f.kind in (FieldKind.SELECT, FieldKind.RADIO,
                                FieldKind.MULTISELECT, FieldKind.COMBOBOX)
                  and f.options]
    if not structured:
        return [], False

    from jobbot.healer.answer import profile_digest
    r = llm.call(
        system=cached_system(
            "You assess whether a candidate's honest answers would trigger an "
            "employer's automatic rejection rule.\n\nCANDIDATE PROFILE:\n"
            + profile_digest(profile)),
        blocks=[{"type": "text", "text":
                 f"Role: {job_title}\n\nStructured screening questions on this form:\n"
                 + json.dumps([f.to_prompt_dict() for f in structured], indent=1)
                 + "\n\nWhich, answered honestly from the profile, would likely "
                   "trigger an automatic rejection? Only flag genuine knockouts "
                   "(hard requirements the candidate does not meet), not questions "
                   "that are merely unfavourable."}],
        tool=KNOCKOUT_TOOL,
        max_tokens=2500,
    )
    d = r.require_tool()
    ko = d.get("knockouts", [])
    if ko:
        log.info("knockout.detected", count=len(ko),
                 recommend_skip=d.get("recommend_skip"),
                 labels=[k.get("label", "")[:40] for k in ko])
    return ko, bool(d.get("recommend_skip"))


# -- checkpoint 2 ---------------------------------------------------------

# What the page itself says about each control's validity. A screenshot cannot
# distinguish a required-field accent from an error outline -- on Greenhouse
# they look alike -- so the verifier was reporting correctly-filled fields as
# blockers on the strength of a border colour, and the heal loop could never
# clear them because nothing was wrong.
_VALIDITY_JS = r"""
(sels) => sels.map((sel) => {
  let el = null;
  try { el = document.querySelector(sel); } catch (e) { return null; }
  if (!el) return null;
  const aria = el.getAttribute('aria-invalid');
  let native = (typeof el.checkValidity === 'function') ? !el.checkValidity() : false;

  // A required checkbox group: Greenhouse marks a "pick at least one" question
  // by putting `required` on every box, and the browser applies that per
  // element -- so each box the candidate did not tick reports invalid with
  // "Please check this box if you want to proceed". Reading that literally
  // said seven of HP IQ's eight sources were errors, and the only way to
  // clear them would be to claim every one of them. One tick answers the
  // question; the siblings are not errors.
  if (native && el.type === 'checkbox' && !el.checked && el.name) {
    const group = [...document.querySelectorAll(
      'input[type="checkbox"][name="' + el.name.replace(/"/g, '\\"') + '"]')];
    if (group.length > 1 && group.some(c => c.checked)) native = false;
  }

  return {
    sel,
    invalid: aria === 'true' || native,
    message: native ? (el.validationMessage || '') : '',
    value_len: (el.value || '').length,
  };
}).filter(Boolean)
"""


async def field_validity(page: Any, form: ParsedForm) -> dict[str, dict[str, Any]]:
    """Per-field validity as the DOM reports it, keyed by field_id."""
    sels = [f.selector for f in form.fields if f.selector]
    if not sels:
        return {}
    try:
        rows = await page.evaluate(_VALIDITY_JS, sels)
    except Exception as exc:  # noqa: BLE001
        log.debug("checkpoint2.validity_unavailable", error=str(exc)[:120])
        return {}
    by_sel = {r["sel"]: r for r in rows}
    return {f.field_id: by_sel[f.selector]
            for f in form.fields if f.selector in by_sel}

def _confirmed_screening(profile) -> str:
    """The screening answers the candidate confirmed, for the verifier only.

    profile_digest deliberately omits these so no model composing an answer can
    read them. The verifier is the opposite case: without them it saw a "Yes"
    on work authorization, found nothing in the profile to check it against,
    and raised a blocker on an answer the candidate had personally confirmed --
    every run, unfixably, because the healer refuses to touch a legal field.
    """
    lines = [f"- {k}: {profile.answer(k).value}"
             for k in sorted(profile.screening)
             if profile.can_answer(k)]
    if not lines:
        return ""
    return ("\n\nSCREENING ANSWERS THE CANDIDATE CONFIRMED (authoritative -- an "
            "entered value matching one of these is correct, not a contradiction):\n"
            + "\n".join(lines))



_TRUNCATED = re.compile(r"truncat|cut off|incomplete|partial|not fully", re.I)


def _norm_ws(v: object) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


async def _dom_value(page: Any, f: FormField) -> str:
    """What the control actually holds, straight from the DOM."""
    if page is None or not f.selector:
        return ""
    try:
        return await page.locator(f.selector).first.input_value(timeout=2000) or ""
    except Exception:  # noqa: BLE001
        try:
            return await page.evaluate(
                "(sel) => { const e = document.querySelector(sel); "
                "return e ? (e.value || e.textContent || '') : ''; }", f.selector)
        except Exception:  # noqa: BLE001
            return ""


async def _dom_truth(page: Any, form: ParsedForm, answers: list[ProposedAnswer],
                     issues: list[VerificationIssue]) -> tuple[list[VerificationIssue], set[str]]:
    """Let the DOM overrule vision on "truncated" text.

    A long essay in a textarea shows its first few lines; the vision pass
    reads that as "visibly truncated" and blocks. Three heal rounds then
    re-type the same complete text and get the same verdict. The DOM holds
    the whole value, so when it matches what we meant to type the issue is
    a warning, not a blocker. Returns the issues and the labels confirmed.
    """
    by_id = {f.field_id: f for f in form.fields}
    intended = {a.field_id: _norm_ws(a.value) for a in answers if a.submittable}
    out: list[VerificationIssue] = []
    confirmed: set[str] = set()
    for i in issues:
        f = by_id.get(i.field_id)
        if (i.severity == "blocker" and f is not None
                and f.kind in (FieldKind.TEXT, FieldKind.TEXTAREA)
                and _TRUNCATED.search(i.problem or "") and i.field_id in intended):
            dom = _norm_ws(await _dom_value(page, f))
            want = intended[i.field_id]
            if dom and (dom == want or (len(dom) >= 0.95 * len(want)
                                        and want.startswith(dom[:200]))):
                log.info("verify.dom_truth", label=f.label[:50], chars=len(dom))
                i = VerificationIssue(i.field_id, i.label,
                                      (i.problem or "") + " (DOM holds the full value; the box scrolls)",
                                      "warning", i.suggested_value)
                confirmed.add(f.label.strip().lower()[:60])
        out.append(i)
    return out, confirmed


async def checkpoint_verify(
    page: Any, llm: LLMClient, profile: Profile, form: ParsedForm,
    answers: list[ProposedAnswer], shots_dir: str | Path, round_no: int = 0,
) -> tuple[Verification, cap.PageCapture]:
    """Re-read the filled form and decide whether it is safe to submit."""
    shots_dir = Path(shots_dir)
    pc = await cap.capture(page, shots_dir, prefix=f"cp2_r{round_no}")
    validity = await field_validity(page, form)

    from jobbot.healer.answer import profile_digest
    intended = []
    by_id = {f.field_id: f for f in form.fields}
    for a in answers:
        f = by_id.get(a.field_id)
        if f is not None and a.submittable:
            row = {"label": f.label[:110], "intended_value": str(a.value)[:200]}
            if f.options:
                # Only a value from this list can be entered. Without it the
                # verifier suggested "Yes" for a dropdown whose three choices
                # were full sentences, and the healer had nothing to apply.
                row["only_these_are_selectable"] = [o.label[:80] for o in f.options[:25]]
            intended.append(row)

    # Required fields we could not answer at all never reach the list above,
    # so the verifier could not see their choices and kept suggesting values
    # that do not exist -- "Yes" for a list of sentences, a date for a list of
    # month-year labels. Filling records what each control offered, so pass
    # those lists along even where there is no intended value.
    unanswered = [
        {"label": f.label[:110],
         "intended_value": "(nothing was entered)",
         "only_these_are_selectable": [o.label[:80] for o in f.options[:25]]}
        for f in form.fields
        if f.required and f.options
        and not any(a.field_id == f.field_id and a.submittable for a in answers)
    ]
    intended.extend(unanswered[:12])

    r = llm.vision(
        system=cached_system(
            "You are the last check before a job application is submitted in the "
            "candidate's name. Be strict and literal.\n\nCANDIDATE PROFILE:\n"
            + profile_digest(profile)
            + _confirmed_screening(profile)),
        prompt=(
            "The screenshots show the application form AFTER it was filled in.\n\n"
            "Values we intended to enter:\n" + json.dumps(intended, indent=1)[:9000]
            + "\n\nAccessibility outline of the filled page:\n"
            + (pc.aria[:16000] or "(unavailable)")
            + _validity_block(form, validity)
            + "\n\nCheck every visible field. Report a problem for anything that is: "
              "empty but required; showing a validation error; holding a value that "
              "contradicts the profile; visibly truncated; or placed in the wrong "
              "field (for example a phone number in a name box). "
              "Set ready_to_submit=true only if there are no blockers."
        ),
        images=pc.tiles,
        tool=VERIFY_FORM_TOOL,
        max_tokens=5000,
    )
    d = r.require_tool()

    label_to_id = {f.label.strip().lower()[:60]: f.field_id for f in form.fields}
    known = set(label_to_id.values())
    issues = []
    for i in d.get("issues", []):
        lab = (i.get("label") or "").strip().lower()[:60]
        fid = i.get("field_id") or ""
        if fid not in known:
            # The model paraphrases ids ("location_city" for candidate-location);
            # an unknown id made the healer skip a blocker it had a fix for.
            fid = label_to_id.get(lab, fid)
        issues.append(VerificationIssue(
            field_id=fid,
            label=i.get("label", ""), problem=i.get("problem", ""),
            severity=i.get("severity", "warning"),
            suggested_value=i.get("suggested_value"),
        ))

    issues, confirmed = await _dom_truth(page, form, answers, issues)
    unfilled = [u for u in (d.get("unfilled_required", []) or [])
                if str(u).strip().lower()[:60] not in confirmed]
    verrs = d.get("validation_errors", []) or []
    ready = bool(d.get("ready_to_submit"))
    if confirmed and not ready and not verrs and not unfilled \
            and not any(i.severity == "blocker" for i in issues):
        # The only thing holding the verdict back was vision misreading a
        # scrolled box; the DOM says the value is whole.
        ready = True
    v = Verification(
        ready_to_submit=ready,
        issues=issues,
        unfilled_required=unfilled,
        validation_errors=verrs,
        summary=d.get("summary", ""),
    )
    log.info("checkpoint2.verified", round=round_no, ready=v.ready_to_submit,
             blockers=len(v.blockers), warnings=len(v.issues) - len(v.blockers),
             unfilled=len(v.unfilled_required))
    return v, pc


def _validity_block(form: ParsedForm, validity: dict[str, dict[str, Any]]) -> str:
    """Tell the verifier what the page reports, and that it outranks a colour."""
    if not validity:
        return ""
    by_id = {f.field_id: f for f in form.fields}
    invalid = [(by_id[k].label, v["message"]) for k, v in validity.items()
               if v["invalid"] and k in by_id]
    lines = [
        "\n\nTHE PAGE'S OWN VALIDATION STATE (authoritative -- this is the form "
        "and the browser reporting on themselves, not an appearance):",
    ]
    if invalid:
        lines.append("These fields are reporting INVALID:")
        lines += [f"  - {lab}: {msg or 'no message given'}" for lab, msg in invalid]
    else:
        lines.append(f"  All {len(validity)} controls report VALID.")
    lines.append(
        "Do not raise a validation blocker for a field the page reports valid. A "
        "coloured border alone is not evidence: required-field accents and error "
        "outlines look alike, and a filled field styled as required is normal. "
        "Report one only where the page says invalid, or where you can read the "
        "actual error text.")
    return "\n".join(lines)


async def heal(
    page: Any, llm: LLMClient, profile: Profile, form: ParsedForm,
    answers: list[ProposedAnswer], shots_dir: str | Path,
    *, max_rounds: int = 4, resume_path: str | Path | None = None,
) -> tuple[Verification, int]:
    """Verify, fix, re-verify until clean or out of rounds.

    Bounded deliberately. An unbounded "fix until perfect" loop against a form
    that cannot be satisfied burns the rate-limit budget and leaves a tab open
    forever; the honest outcome there is to stop and flag for a human.
    """
    from jobbot.forms.fill import apply_answer

    by_id = {f.field_id: f for f in form.fields}
    rounds = 0
    v = Verification()
    det_by_id: dict[str, Any] | None = None   # profile answers, computed on demand

    for rounds in range(1, max_rounds + 1):
        v, _ = await checkpoint_verify(page, llm, profile, form, answers,
                                       shots_dir, round_no=rounds)
        if v.ready_to_submit and not v.blockers:
            log.info("heal.clean", rounds=rounds)
            return v, rounds

        fixed = 0
        for issue in v.blockers:
            f = by_id.get(issue.field_id)
            if f is None:
                continue
            value: Any = None
            source, confidence, why = AnswerSource.COMPOSED, 0.6, "healer fix"
            if f.profile_key is None:
                # The fill stage classifies every field in place; a field that
                # reaches the healer unclassified (a late-appearing step, a
                # test) must still be recognised as legally significant.
                from jobbot.healer.answer import classify
                classify(f)
            if f.profile_key in LEGALLY_SIGNIFICANT:
                # Never let the healer COMPOSE a legally significant answer.
                #
                # Keyed on the profile's own denylist, not on the vision pass's
                # legally_significant flag: vision marked "End date month" and
                # "End date year" on an education block as legally significant,
                # the healer refused both, and the application could not reach
                # ready_to_submit however many rounds it ran. What must never be
                # model-authored is the fixed set of status questions, and those
                # all carry a profile_key.
                #
                # A confirmed profile value that simply did not stick -- a
                # consent checkbox that ignored the first click, a combobox
                # that closed early -- is the candidate's own answer, not the
                # model's, and may be applied again verbatim.
                if det_by_id is None:
                    from jobbot.healer.answer import deterministic_answers
                    det_by_id = {a.field_id: a for a in deterministic_answers(profile, form)[0]
                                 if a.submittable}
                again = det_by_id.get(f.field_id)
                if again is None:
                    log.warning("heal.refused_legal", label=f.label[:60],
                                key=f.profile_key)
                    continue
                log.info("heal.reapplied_profile", label=f.label[:60], key=f.profile_key)
                value = again.value
                source, confidence, why = AnswerSource.PROFILE, 1.0, "profile value re-applied"
            if value is None and issue.suggested_value in (None, ""):
                continue
            if value is None and _looks_like_a_description(str(issue.suggested_value)):
                # The verifier sometimes answers with a description of the value
                # ("Candidate's real phone number") instead of the value. Typing
                # that into a live form is worse than leaving it blank.
                log.warning("heal.rejected_placeholder", label=f.label[:50],
                            suggested=str(issue.suggested_value)[:60])
                continue
            if value is None:
                value = issue.suggested_value
            if f.options:
                # Only a value from the list can be entered, and the verifier
                # does invent ones that are not on it: for "How did you hear
                # about this job?" it suggested "Company website / Careers
                # page" where the six real choices were Grace Hopper, a career
                # fair, word of mouth, Cloudflare social media, LinkedIn and
                # Google. Snap it to a real option, and where nothing is close,
                # let the model choose from the list rather than apply a value
                # the control cannot hold.
                labels = f.option_labels()
                snapped, score, _ = match_option(str(value), labels)
                if snapped is None and is_decline(value):
                    # "I do not wish to answer" vs "Decline To Self Identify":
                    # the filler already resolves these; the healer must too,
                    # or every EEO field the verifier flags stays flagged.
                    snapped = match_decline(labels)
                    if snapped is not None:
                        log.info("heal.decline_synonym", label=f.label[:50], chose=snapped)
                if snapped is None:
                    from jobbot.healer.answer import model_answers
                    picked = await asyncio.to_thread(
                        model_answers, llm, profile, [f],
                        job_context="Choose one of this field's listed options, verbatim.")
                    snapped = next(
                        (str(a.value) for a in picked
                         if a.submittable and match_option(str(a.value), labels)[0]),
                        None)
                    if snapped is not None:
                        log.info("heal.model_chose_option", label=f.label[:50],
                                 chose=snapped[:60])
                if snapped is None:
                    log.warning("heal.no_valid_option", label=f.label[:50],
                                suggested=str(value)[:50], options=labels[:6])
                    continue
                value = snapped

            patch = ProposedAnswer(f.field_id, value, source, confidence, why)
            if await apply_answer(page, f, patch, resume_path=resume_path):
                # In place, not a rebind: the caller records this list as the
                # ledger of what was actually entered, so a healed value that
                # only existed in a local copy would never be recorded.
                answers[:] = [a for a in answers if a.field_id != f.field_id] + [patch]
                fixed += 1

        # Voluntary self-identification the candidate never provided.
        #
        # Greenhouse's EEO block (Gender, "Please identify your race",
        # Hispanic/Latino) is optional, but the verifier lists it in
        # unfilled_required, and those carry no field_id so the blocker loop
        # above never sees them. The profile holds no gender or race, and it
        # must not: this is not a fact to invent. Declining is the honest,
        # privacy-preserving non-answer every such field offers, and it is not
        # legally significant -- unlike veteran or disability status, which are
        # answered only from the profile and are skipped here.
        label_to_field = {f.label.strip().lower(): f for f in form.fields}
        for label in v.unfilled_required:
            key = str(label).strip().lower()
            f = label_to_field.get(key) or next(
                (ff for lab, ff in label_to_field.items()
                 if key and (key in lab or lab in key)), None)
            if f is None or not f.options:
                continue
            if any(a.field_id == f.field_id and a.submittable for a in answers):
                continue
            if f.profile_key is None:
                from jobbot.healer.answer import classify
                classify(f)
            if f.profile_key in LEGALLY_SIGNIFICANT:
                continue
            decline = match_decline(f.option_labels())
            if decline is None:
                continue
            patch = ProposedAnswer(f.field_id, decline, AnswerSource.DERIVED, 0.9,
                                   "voluntary self-ID left unprovided; declined, not invented")
            if await apply_answer(page, f, patch, resume_path=resume_path):
                answers[:] = [a for a in answers if a.field_id != f.field_id] + [patch]
                fixed += 1
                log.info("heal.declined_self_id", label=f.label[:50], chose=decline)

        log.info("heal.round", round=rounds, blockers=len(v.blockers), fixed=fixed)
        if fixed == 0:
            break

    return v, rounds


# -- checkpoint 3 ---------------------------------------------------------

async def checkpoint_outcome(
    page: Any, llm: LLMClient, shots_dir: str | Path,
) -> tuple[Outcome, cap.PageCapture]:
    """One call, after submit. Did it actually go through?"""
    shots_dir = Path(shots_dir)
    pc = await cap.capture(page, shots_dir, prefix="cp3", max_tiles=3)

    r = llm.vision(
        system=cached_system(READER_SYSTEM),
        prompt=(
            "This is the page AFTER clicking submit on a job application.\n\n"
            "Decide whether the application was actually submitted. Do NOT assume "
            "success because a button was clicked. Set submitted=true only with "
            "affirmative on-page evidence: a confirmation message, a reference or "
            "application number, or a state indicating we have already applied. "
            "If the form is still showing with fields and a submit button, it was "
            "not submitted.\n\nAlso record concise, reusable lessons for future "
            "applications on this ATS.\n\nAccessibility outline:\n"
            + (pc.aria[:10000] or "(unavailable)")
        ),
        images=pc.tiles,
        tool=POST_SUBMIT_TOOL,
        max_tokens=3000,
    )
    d = r.require_tool()
    o = Outcome(
        submitted=bool(d.get("submitted")),
        evidence=(d.get("evidence") or "")[:600],
        still_on_form=bool(d.get("still_on_form", True)),
        errors=d.get("errors_shown", []) or [],
        answers_correct=bool(d.get("answers_correct", True)),
        lessons=d.get("lessons", []) or [],
    )
    log.info("checkpoint3.outcome", submitted=o.submitted,
             evidence=o.evidence[:80], lessons=len(o.lessons))
    return o, pc
