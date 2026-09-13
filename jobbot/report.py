"""Human-readable dump of everything one application produced.

The point of this file is accountability. A system that fills forms in someone's
name has to be able to show, field by field, exactly what it said and where each
answer came from -- before it is trusted to submit anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def report(audit_dir: str | Path) -> str:
    d = Path(audit_dir)
    out: list[str] = []
    w = out.append

    form = _load(d / "form.json") or {}
    answers = _load(d / "answers.json") or []
    verification = _load(d / "verification.json") or {}
    critique = _load(d / "resume_critique.json") or []
    resume = _load(d / "resume_content.json") or {}
    smoke = _load(d / "project_smoke.json") or {}

    w("=" * 78)
    w("APPLICATION REPORT")
    w("=" * 78)
    w(f"audit dir : {d}")
    if form:
        w(f"form      : {len(form.get('fields', []))} fields, submit = {form.get('submit')!r}")

    if critique:
        w("")
        w("-" * 78)
        w("RESUME TAILORING")
        w("-" * 78)
        for h in critique:
            if "final_ats_score" in h:
                w(f"  FINAL match score : {h['final_ats_score']}/100")
                w(f"    keyword match   : {h.get('keyword_match')}")
                w(f"    skills coverage : {h.get('skills_coverage')}")
                if h.get("injectable_missed"):
                    w(f"    still missing   : {', '.join(h['injectable_missed'])}")
                for r in h.get("recommendations", [])[:3]:
                    w(f"    note            : {r[:110]}")
            else:
                w(f"  round {h.get('round')}: verdict={h.get('verdict')} "
                  f"revisions={h.get('revisions')} applied={h.get('applied')} "
                  f"score={h.get('ats_score')}")

    if resume:
        w("")
        w("-" * 78)
        w("TAILORED RESUME CONTENT")
        w("-" * 78)
        if resume.get("summary"):
            w(f"  SUMMARY: {resume['summary']}")
        for cat, items in (resume.get("skills") or {}).items():
            if isinstance(items, list):
                w(f"  {cat}: {', '.join(items)}")
        for e in resume.get("experience", []):
            w(f"  {e.get('title')} @ {e.get('company')}  ({e.get('dates')})")
            for b in e.get("bullets", []):
                w(f"     - {b}")
        for p_ in resume.get("projects", []):
            w(f"  PROJECT {p_.get('name')} {p_.get('url','')}")
            for b in p_.get("bullets", []):
                w(f"     - {b}")

    if smoke:
        w("")
        w("-" * 78)
        w("GENERATED PROJECT SMOKE TEST")
        w("-" * 78)
        w(f"  command: {smoke.get('command')}")
        w(f"  passed : {smoke.get('passed')}")
        w(f"  output : {(smoke.get('output') or '')[-300:]}")

    if answers:
        w("")
        w("-" * 78)
        w("EVERY ANSWER ENTERED INTO THE FORM")
        w("-" * 78)
        for a in answers:
            flag = ""
            if a.get("needs_human"):
                flag = "  <-- LEFT BLANK: " + (a.get("blocked_reason") or "")[:60]
            w(f"  [{a.get('source','?'):9}] {a.get('label','')[:56]}")
            w(f"      = {str(a.get('value'))[:150]!r}{flag}")
            if a.get("rationale"):
                w(f"        why: {a['rationale'][:110]}")

    if verification:
        w("")
        w("-" * 78)
        w("PRE-SUBMIT VERIFICATION (checkpoint 2)")
        w("-" * 78)
        w(f"  ready to submit : {verification.get('ready')}")
        w(f"  heal rounds     : {verification.get('heal_rounds')}")
        if verification.get("summary"):
            w(f"  summary         : {verification['summary'][:300]}")
        for i in verification.get("issues", []):
            w(f"  [{i.get('severity','?'):8}] {i.get('label','')[:50]}: {i.get('problem','')[:100]}")
        if verification.get("unfilled_required"):
            w(f"  unfilled required: {verification['unfilled_required']}")
        if verification.get("validation_errors"):
            w(f"  page errors     : {verification['validation_errors']}")

    return "\n".join(out)


if __name__ == "__main__":
    import sys
    print(report(sys.argv[1]))
