"""Extract answerable controls from the live DOM.

Vision tells us what a human sees; this tells us what we can actually click.
Both are needed: the DOM is authoritative for selectors and option values, the
screenshot is authoritative for what is genuinely visible and how the page reads.

Label resolution goes through `label[for=id]`, then `aria-labelledby`, then
`aria-label`, then a wrapping `<label>`, and only then falls back to proximity.
That ordering is the whole point. Proximity heuristics are what produce the
classic "phone number written into Last Name" bug -- a silent data corruption
that is strictly worse than crashing, because it submits.
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog

from jobbot.forms.model import FieldKind, FieldOption, FormField, ParsedForm

log = structlog.get_logger(__name__)

# Runs in page context. Returns a plain JSON description of every control.
_EXTRACT_JS = r"""
() => {
  const vis = (el) => {
    if (!el) return false;
    // File inputs first, before any style test. ATSes routinely render a styled
    // dropzone and hide the real <input type=file> with display:none -- it is
    // still fully functional via set_input_files, and dropping it means the
    // resume silently never attaches.
    if (el.type === 'file') return true;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const labelFor = (el) => {
    // 1. explicit label[for=id] -- the only join that cannot mis-associate
    if (el.id) {
      const esc = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id.replace(/"/g, '\\"');
      const l = document.querySelector(`label[for="${esc}"]`);
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    // 2. aria-labelledby
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const txt = lb.split(/\s+/).map(id => {
        const n = document.getElementById(id);
        return n ? n.innerText.trim() : '';
      }).filter(Boolean).join(' ');
      if (txt) return txt;
    }
    // 3. aria-label
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    // 4. wrapping <label>
    const wrap = el.closest('label');
    if (wrap && wrap.innerText.trim()) return wrap.innerText.trim();
    // 5. fieldset legend (radio/checkbox groups)
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg && lg.innerText.trim()) return lg.innerText.trim();
    }
    // 6. last resort: nearest preceding text. Marked so callers can distrust it.
    let p = el.parentElement, hops = 0;
    while (p && hops < 3) {
      const t = Array.from(p.childNodes)
        .filter(n => n.nodeType === 3).map(n => n.textContent.trim())
        .filter(Boolean).join(' ');
      if (t) return '~' + t;
      p = p.parentElement; hops++;
    }
    return '';
  };

  const section = (el) => {
    let p = el.parentElement;
    while (p) {
      const h = p.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > legend');
      if (h && h.innerText.trim()) return h.innerText.trim().slice(0, 80);
      p = p.parentElement;
    }
    return '';
  };

  const cssPath = (el) => {
    if (el.id) {
      const esc = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
      return `#${esc}`;
    }
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 6) {
      let sel = n.tagName.toLowerCase();
      if (n.name) { sel += `[name="${n.name}"]`; parts.unshift(sel); break; }
      const par = n.parentElement;
      if (par) {
        const same = Array.from(par.children).filter(c => c.tagName === n.tagName);
        if (same.length > 1) sel += `:nth-of-type(${same.indexOf(n) + 1})`;
      }
      parts.unshift(sel);
      n = n.parentElement;
    }
    return parts.join(' > ');
  };

  const out = [];
  const seenGroup = new Set();
  const nodes = document.querySelectorAll('input, textarea, select, [role="combobox"], [role="radiogroup"], [contenteditable="true"]');

  for (const el of nodes) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'reset') continue;
    if (!vis(el)) continue;

    let kind = 'text';
    let options = [];

    if (tag === 'select') {
      kind = el.multiple ? 'multiselect' : 'select';
      options = Array.from(el.options)
        .filter(o => o.value !== '' || o.textContent.trim())
        .map(o => ({ label: o.textContent.trim(), value: o.value }));
    } else if (tag === 'textarea' || el.getAttribute('contenteditable') === 'true') {
      kind = 'textarea';
    } else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) {
      kind = 'combobox';
      const owns = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
      if (owns) {
        const box = document.getElementById(owns);
        if (box) options = Array.from(box.querySelectorAll('[role="option"]'))
          .map(o => ({ label: o.innerText.trim(), value: o.getAttribute('data-value') || o.innerText.trim() }));
      }
    } else if (type === 'radio') {
      kind = 'radio';
      const name = el.name || labelFor(el);
      if (seenGroup.has('r:' + name)) continue;
      seenGroup.add('r:' + name);
      const group = name ? document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`) : [el];
      options = Array.from(group).map(r => ({ label: labelFor(r).replace(/^~/, ''), value: r.value }));
    } else if (type === 'checkbox') {
      kind = 'checkbox';
    } else if (type === 'file') {
      kind = 'file';
    } else if (type === 'email') { kind = 'email'; }
    else if (type === 'tel')     { kind = 'phone'; }
    else if (type === 'number')  { kind = 'number'; }
    else if (type === 'date')    { kind = 'date'; }

    const raw = labelFor(el);
    out.push({
      label: raw.replace(/^~/, '').replace(/\s*\*\s*$/, '').trim(),
      label_is_proximity: raw.startsWith('~'),
      kind, options,
      name: el.name || '',
      dom_id: el.id || '',
      required: el.required || el.getAttribute('aria-required') === 'true',
      placeholder: el.getAttribute('placeholder') || '',
      value: (el.value !== undefined ? String(el.value) : '').slice(0, 300),
      checked: !!el.checked,
      maxlength: el.maxLength > 0 ? el.maxLength : null,
      selector: cssPath(el),
      section: section(el),
    });
  }

  const submit = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'))
    .filter(vis)
    .map(b => (b.innerText || b.value || '').trim())
    .filter(t => /submit|apply|continue|next|save/i.test(t));

  return { fields: out, submit_candidates: submit, title: document.title, url: location.href };
}
"""


def _kind(raw: str) -> FieldKind:
    try:
        return FieldKind(raw)
    except ValueError:
        return FieldKind.UNKNOWN


def _field_id(f: dict[str, Any]) -> str:
    """Stable handle: prefer the DOM id/name, else hash the label+selector."""
    if f.get("dom_id"):
        return f["dom_id"]
    if f.get("name"):
        return f["name"]
    basis = f"{f.get('label','')}|{f.get('selector','')}"
    return "f_" + hashlib.sha256(basis.encode()).hexdigest()[:10]


async def extract_form(page: Any) -> ParsedForm:
    """Read every answerable control on the current page."""
    data = await page.evaluate(_EXTRACT_JS)

    fields: list[FormField] = []
    for f in data.get("fields", []):
        label = f.get("label") or f.get("placeholder") or f.get("name") or ""
        if not label:
            # Never drop a file input for lacking a label -- several ATSes ship a
            # bare hidden <input type=file> behind a styled dropzone, and losing
            # it means the resume silently never attaches.
            if f.get("kind") == "file":
                label = "Resume / CV"
            else:
                continue
        fields.append(FormField(
            field_id=_field_id(f),
            label=label[:300],
            kind=_kind(f.get("kind", "text")),
            required=bool(f.get("required")),
            options=[FieldOption(label=o["label"], value=o.get("value", ""))
                     for o in f.get("options", []) if o.get("label")],
            placeholder=(f.get("placeholder") or "")[:200],
            current_value=(f.get("value") or "")[:300],
            group=(f.get("section") or "")[:120],
            max_length=f.get("maxlength"),
            selector=f.get("selector", ""),
        ))

    subs = data.get("submit_candidates") or []
    submit = next((s for s in subs if "submit" in s.lower() or "apply" in s.lower()),
                  subs[0] if subs else "")

    form = ParsedForm(
        fields=fields,
        submit_label=submit,
        page_title=data.get("title", ""),
    )
    log.info("extract.done", fields=len(fields), submit=submit,
             proximity_labels=sum(1 for f in data.get("fields", []) if f.get("label_is_proximity")))
    return form
