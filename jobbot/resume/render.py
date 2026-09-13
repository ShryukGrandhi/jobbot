"""Render a resume to a strictly one-page, ATS-safe PDF.

Rendered through the browser we already run, via CDP `Page.printToPDF`. That
buys exact typographic control, a real text layer, and -- crucially -- the
ability to measure the true page count and iterate until it is one.

The layout rules are chosen so a parser can read it. There is no controlled
public study proving any single rule, but the mechanism is not in doubt: PDF
text extraction follows drawing order, and images carry no text layer at all.
So:

  * one column, no tables, no text boxes, no floats
  * nothing in the page header or footer region -- parsers routinely drop it
  * standard section headings ("Experience", "Education", "Skills")
  * no icons, no graphics, no photo
  * real selectable text, never an image of text

The one-page cap is enforced by measurement, not by guessing at content length:
render, count pages, tighten, repeat.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class Density:
    """Knobs the fitter turns, loosest first."""
    base_pt: float = 10.5
    line: float = 1.30
    section_gap_pt: float = 7.0
    entry_gap_pt: float = 5.0
    margin_in: float = 0.5
    max_bullets: int = 5
    show_summary: bool = True

    def tighter(self) -> "Density":
        """One notch denser. Drops content only after squeezing whitespace."""
        d = Density(**self.__dict__)
        if d.line > 1.16:
            d.line = round(d.line - 0.05, 3)
        elif d.section_gap_pt > 4.0:
            d.section_gap_pt -= 1.0
            d.entry_gap_pt = max(2.5, d.entry_gap_pt - 0.75)
        elif d.base_pt > 9.0:
            d.base_pt = round(d.base_pt - 0.25, 2)
        elif d.margin_in > 0.38:
            d.margin_in = round(d.margin_in - 0.04, 3)
        elif d.max_bullets > 2:
            d.max_bullets -= 1
        elif d.show_summary:
            d.show_summary = False
        return d


CSS = """
@page {{ size: Letter; margin: {margin_in}in; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  font-family: Georgia, 'Times New Roman', Times, serif;
  font-size: {base_pt}pt;
  line-height: {line};
  color: #111;
  -webkit-font-smoothing: antialiased;
}}
a {{ color: #111; text-decoration: none; }}
h1 {{ font-size: {name_pt}pt; margin: 0 0 2pt 0; letter-spacing: .3px; font-weight: 700; }}
.contact {{ font-size: {small_pt}pt; margin-bottom: {section_gap}pt; }}
.contact .sep {{ padding: 0 4pt; color: #555; }}
h2 {{
  font-size: {h2_pt}pt; text-transform: uppercase; letter-spacing: .8px;
  border-bottom: .75pt solid #333; margin: {section_gap}pt 0 3pt 0;
  padding-bottom: 1.5pt; font-weight: 700;
}}
.entry {{ margin-bottom: {entry_gap}pt; }}
.row {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8pt; }}
.role {{ font-weight: 700; }}
.org {{ font-style: italic; }}
.dates {{ font-size: {small_pt}pt; white-space: nowrap; color: #333; }}
ul {{ margin: 2pt 0 0 0; padding-left: 12pt; }}
li {{ margin-bottom: 1pt; }}
.skills div {{ margin-bottom: 1.5pt; }}
.skills b {{ font-weight: 700; }}
p.summary {{ margin: 0 0 {section_gap}pt 0; }}
"""


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html(data: dict[str, Any], d: Density) -> str:
    """Assemble the resume document. `data` is the tailored content."""
    css = CSS.format(
        margin_in=d.margin_in, base_pt=d.base_pt, line=d.line,
        name_pt=round(d.base_pt * 1.85, 2), h2_pt=round(d.base_pt * 0.98, 2),
        small_pt=round(d.base_pt * 0.9, 2),
        section_gap=d.section_gap_pt, entry_gap=d.entry_gap_pt,
    )
    ident = data.get("identity", {})
    contact_bits = [ident.get("location", ""), ident.get("email", ""),
                    ident.get("phone", ""), ident.get("linkedin", ""),
                    ident.get("github", ""), ident.get("website", "")]
    bits = [f"<span>{_esc(c)}</span>" for c in contact_bits if c]
    contact = "<span class='sep'>|</span>".join(bits)

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<style>{css}</style></head><body>",
        f"<h1>{_esc(ident.get('name',''))}</h1>",
        f"<div class='contact'>{contact}</div>",
    ]

    if d.show_summary and data.get("summary"):
        parts.append(f"<p class='summary'>{_esc(data['summary'])}</p>")

    if data.get("skills"):
        parts.append("<h2>Skills</h2><div class='skills'>")
        for cat, items in data["skills"].items():
            if items:
                parts.append(f"<div><b>{_esc(cat)}:</b> {_esc(', '.join(items))}</div>")
        parts.append("</div>")

    if data.get("experience"):
        parts.append("<h2>Experience</h2>")
        for e in data["experience"]:
            parts.append("<div class='entry'><div class='row'>"
                         f"<div><span class='role'>{_esc(e.get('title',''))}</span>, "
                         f"<span class='org'>{_esc(e.get('company',''))}</span></div>"
                         f"<div class='dates'>{_esc(e.get('dates',''))}</div></div>")
            bullets = (e.get("bullets") or [])[: d.max_bullets]
            if bullets:
                parts.append("<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
            parts.append("</div>")

    if data.get("projects"):
        parts.append("<h2>Projects</h2>")
        for p in data["projects"]:
            link = f" &mdash; {_esc(p.get('url',''))}" if p.get("url") else ""
            parts.append(f"<div class='entry'><div class='row'><div>"
                         f"<span class='role'>{_esc(p.get('name',''))}</span>{link}</div></div>")
            bullets = (p.get("bullets") or [])[: max(2, d.max_bullets - 1)]
            if bullets:
                parts.append("<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
            parts.append("</div>")

    if data.get("awards"):
        parts.append("<h2>Awards &amp; Publications</h2><ul>")
        for a in data["awards"][:4]:
            parts.append(f"<li>{_esc(a)}</li>")
        parts.append("</ul>")

    if data.get("education"):
        parts.append("<h2>Education</h2>")
        for ed in data["education"]:
            parts.append("<div class='entry'><div class='row'>"
                         f"<div><span class='role'>{_esc(ed.get('degree',''))}</span>"
                         f"{', ' + _esc(ed.get('school','')) if ed.get('school') else ''}</div>"
                         f"<div class='dates'>{_esc(ed.get('dates',''))}</div></div></div>")

    parts.append("</body></html>")
    return "".join(parts)


async def render_pdf(page: Any, html: str, dest: str | Path) -> int:
    """Print `html` to `dest` via CDP. Returns the page count."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # NOTE: page.set_content() hangs under this Chromium build regardless of
    # wait_until, so we write the document to a temp file and navigate to it.
    # Navigation is also a truer render path than injecting content.
    tmp = dest.with_suffix(".src.html")
    tmp.write_text(html, encoding="utf-8")
    await page.goto(tmp.resolve().as_uri(), wait_until="domcontentloaded")
    await page.wait_for_timeout(150)
    cdp = await page.context.new_cdp_session(page)
    res = await cdp.send("Page.printToPDF", {
        "printBackground": True,
        "preferCSSPageSize": True,
        "marginTop": 0, "marginBottom": 0, "marginLeft": 0, "marginRight": 0,
        "displayHeaderFooter": False,   # nothing in the header/footer region
    })
    dest.write_bytes(base64.b64decode(res["data"]))
    try:
        await cdp.detach()
    except Exception:  # noqa: BLE001
        pass
    tmp.unlink(missing_ok=True)

    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(dest))
    n = len(pdf)
    pdf.close()
    return n


async def render_one_page(
    page: Any, data: dict[str, Any], dest: str | Path, *, max_attempts: int = 22,
) -> tuple[Path, Density, int]:
    """Render, measure, tighten, repeat until it genuinely fits on one page."""
    dest = Path(dest)
    d = Density()
    for attempt in range(1, max_attempts + 1):
        pages = await render_pdf(page, build_html(data, d), dest)
        if pages <= 1:
            log.info("resume.fits", attempts=attempt, base_pt=d.base_pt,
                     line=d.line, bullets=d.max_bullets, summary=d.show_summary)
            return dest, d, attempt
        log.debug("resume.too_long", attempt=attempt, pages=pages, base_pt=d.base_pt)
        d = d.tighter()

    log.warning("resume.could_not_fit", attempts=max_attempts)
    return dest, d, max_attempts


def extract_text(pdf_path: str | Path) -> str:
    """Read the PDF's text layer back -- the same way a parser would."""
    import pdfplumber
    out = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pg in pdf.pages:
            out.append(pg.extract_text() or "")
    return "\n".join(out)
