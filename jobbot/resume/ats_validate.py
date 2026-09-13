"""Validate a generated resume against real ATS parsers.

Two layers:

  * Offline: read the PDF's own text layer back, exactly as a parser would, and
    assert the facts that matter survive extraction. Catches the classic killers
    -- multi-column layout, text rendered as an image, content stranded in a page
    header -- without touching anyone's servers.

  * Live: upload to a real ATS application form and read back what its parser
    populated. This is the only test that measures the thing we actually care
    about. Nothing is ever submitted; we attach a file and observe autofill.

Lever parses client-side on upload and is therefore the usable live target.
Greenhouse accepts the attachment but parses server-side after submission, so it
can only confirm upload, not extraction.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class ParseReport:
    source: str
    fields: dict[str, str] = field(default_factory=dict)
    expected: dict[str, str] = field(default_factory=dict)
    matched: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    uploaded: bool = False
    error: str = ""

    @property
    def score(self) -> float:
        total = len(self.matched) + len(self.missed)
        return round(len(self.matched) / total, 3) if total else 0.0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9@.]+", "", (s or "").lower())


def offline_check(pdf_path: str | Path, expect: dict[str, str]) -> ParseReport:
    """Assert the text layer contains what a parser needs."""
    from jobbot.resume.render import extract_text

    rep = ParseReport(source="offline-textlayer", expected=expect)
    try:
        text = extract_text(pdf_path)
    except Exception as exc:  # noqa: BLE001
        rep.error = str(exc)[:200]
        return rep

    flat = _norm(text)
    for key, val in expect.items():
        if not val:
            continue
        if _norm(val) in flat:
            rep.matched.append(key)
        else:
            rep.missed.append(key)
    rep.fields["chars_extracted"] = str(len(text))
    return rep


async def live_lever(page: Any, apply_url: str, pdf_path: str | Path,
                     expect: dict[str, str], settle_rounds: int = 12) -> ParseReport:
    """Upload to a real Lever form and read back what its parser filled in."""
    from jobbot.browser import capture as cap
    from jobbot.forms.extract import extract_form
    from jobbot.forms.fill import upload_file

    rep = ParseReport(source="lever-live", expected=expect)
    try:
        await page.goto(apply_url, wait_until="domcontentloaded")
        await cap.settle(page, quiet_ms=1800)
        await cap.dismiss_overlays(page)

        form = await extract_form(page)
        rf = next((f for f in form.fields if f.kind.value == "file"), None)
        if rf is None:
            rep.error = "no file input found on the page"
            return rep

        watch = {f.label.split("\n")[0][:30]: f.selector
                 for f in form.fields
                 if f.kind.value in ("text", "email", "phone") and f.selector}

        rep.uploaded = await upload_file(page, rf, pdf_path)
        if not rep.uploaded:
            rep.error = "upload rejected"
            return rep

        for _ in range(settle_rounds):
            await asyncio.sleep(2.5)
            vals = {}
            for k, sel in watch.items():
                try:
                    vals[k] = await page.locator(sel).first.input_value()
                except Exception:  # noqa: BLE001
                    vals[k] = ""
            if any(vals.values()):
                rep.fields = {k: v for k, v in vals.items() if v}
                break

        flat = _norm(" ".join(rep.fields.values()))
        for key, val in expect.items():
            if not val:
                continue
            if _norm(val) in flat:
                rep.matched.append(key)
            else:
                rep.missed.append(key)

    except Exception as exc:  # noqa: BLE001
        rep.error = str(exc)[:250]
    return rep


def print_report(rep: ParseReport) -> None:
    print(f"\n[{rep.source}]  score {rep.score:.2f}  "
          f"({len(rep.matched)}/{len(rep.matched) + len(rep.missed)})")
    if rep.error:
        print(f"  error: {rep.error}")
    if rep.fields:
        print("  parser returned:")
        for k, v in rep.fields.items():
            print(f"    {k:<26} {v[:60]}")
    if rep.matched:
        print(f"  matched: {', '.join(rep.matched)}")
    if rep.missed:
        print(f"  MISSED : {', '.join(rep.missed)}")
