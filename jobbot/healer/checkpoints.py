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

import json
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import structlog

from jobbot.browser import capture as cap
from jobbot.forms.model import (
    FieldKind, FormField, ParsedForm, ProposedAnswer,
    Verification, VerificationIssue,
)
from jobbot.forms.extract import extract_form
from jobbot.llm.client import LLMClient, cached_system
from jobbot.llm.schemas import (
    KNOCKOUT_TOOL, PARSE_FORM_TOOL, POST_SUBMIT_TOOL, VERIFY_FORM_TOOL,
)
from jobbot.profile import Profile

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


_DESCRIPTION_MARKERS = re.compile(
    r"candidate'?s|the user'?s|real |actual |valid |their |your |"
    r"\bplaceholder\b|\bTBD\b|\bN/?A\b|<[^>]+>|\[[^\]]+\]|"
    r"should be|must be|needs to be|enter (a|the|your)",
    re.I,
)


def _looks_like_a_description(v: str) -> bool:
    """True when the model described a value instead of supplying one."""
    v = (v or "").strip()
    if not v:
        return True
    return bool(_DESCRIPTION_MARKERS.search(v))


def _merge(dom: ParsedForm, seen: dict[str, Any]) -> ParsedForm:
    """Reconcile the vision reading with the DOM reading.

    The DOM owns selectors and option values. Vision owns visibility, required
    markers, and legal significance. Where vision reports a field the DOM did
    not surface, keep it -- flagged, since we have no selector for it yet.
    """
    by_label = {f.label.strip().lower()[:60]: f for f in dom.fields}

    for vf in seen.get("fields", []):
        key = (vf.get("label") or "").strip().lower()[:60]
        match = by_label.get(key)
        if match is None:
            for lab, f in by_label.items():
                if lab and (lab in key or key in lab) and min(len(lab), len(key)) > 12:
                    match = f
                    break
        if match is not None:
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

async def checkpoint_verify(
    page: Any, llm: LLMClient, profile: Profile, form: ParsedForm,
    answers: list[ProposedAnswer], shots_dir: str | Path, round_no: int = 0,
) -> tuple[Verification, cap.PageCapture]:
    """Re-read the filled form and decide whether it is safe to submit."""
    shots_dir = Path(shots_dir)
    pc = await cap.capture(page, shots_dir, prefix=f"cp2_r{round_no}")

    from jobbot.healer.answer import profile_digest
    intended = []
    by_id = {f.field_id: f for f in form.fields}
    for a in answers:
        f = by_id.get(a.field_id)
        if f is not None and a.submittable:
            intended.append({"label": f.label[:110], "intended_value": str(a.value)[:200]})

    r = llm.vision(
        system=cached_system(
            "You are the last check before a job application is submitted in the "
            "candidate's name. Be strict and literal.\n\nCANDIDATE PROFILE:\n"
            + profile_digest(profile)),
        prompt=(
            "The screenshots show the application form AFTER it was filled in.\n\n"
            "Values we intended to enter:\n" + json.dumps(intended, indent=1)[:9000]
            + "\n\nAccessibility outline of the filled page:\n"
            + (pc.aria[:16000] or "(unavailable)")
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
    issues = []
    for i in d.get("issues", []):
        lab = (i.get("label") or "").strip().lower()[:60]
        issues.append(VerificationIssue(
            field_id=i.get("field_id") or label_to_id.get(lab, ""),
            label=i.get("label", ""), problem=i.get("problem", ""),
            severity=i.get("severity", "warning"),
            suggested_value=i.get("suggested_value"),
        ))

    v = Verification(
        ready_to_submit=bool(d.get("ready_to_submit")),
        issues=issues,
        unfilled_required=d.get("unfilled_required", []) or [],
        validation_errors=d.get("validation_errors", []) or [],
        summary=d.get("summary", ""),
    )
    log.info("checkpoint2.verified", round=round_no, ready=v.ready_to_submit,
             blockers=len(v.blockers), warnings=len(v.issues) - len(v.blockers),
             unfilled=len(v.unfilled_required))
    return v, pc


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
            if f.legally_significant:
                # Never let the healer rewrite a legally significant answer.
                log.warning("heal.refused_legal", label=f.label[:60])
                continue
            if issue.suggested_value in (None, ""):
                continue
            if _looks_like_a_description(str(issue.suggested_value)):
                # The verifier sometimes answers with a description of the value
                # ("Candidate's real phone number") instead of the value. Typing
                # that into a live form is worse than leaving it blank.
                log.warning("heal.rejected_placeholder", label=f.label[:50],
                            suggested=str(issue.suggested_value)[:60])
                continue
            from jobbot.forms.model import AnswerSource
            patch = ProposedAnswer(f.field_id, issue.suggested_value,
                                   AnswerSource.COMPOSED, 0.6, "healer fix")
            if await apply_answer(page, f, patch, resume_path=resume_path):
                # In place, not a rebind: the caller records this list as the
                # ledger of what was actually entered, so a healed value that
                # only existed in a local copy would never be recorded.
                answers[:] = [a for a in answers if a.field_id != f.field_id] + [patch]
                fixed += 1

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
