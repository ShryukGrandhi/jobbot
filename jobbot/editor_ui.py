"""The profile editor page: markup, styles and the small amount of JS it needs.

Kept apart from `editor.py` so the parsing/validation/save logic -- the part
that decides what gets written in the user's name -- is readable without
scrolling past a page of HTML.
"""

from __future__ import annotations

import html
import json
from typing import Any

CSS = """
:root{--bg:#0c0d10;--panel:#14161b;--panel2:#191c22;--line:#22252c;--fg:#d7dae0;
--dim:#7c828e;--ok:#5ec27a;--warn:#e0b341;--bad:#e0605e;--acc:#6aa9f0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
a{color:var(--acc)}
header{position:sticky;top:0;z-index:20;background:var(--bg);
border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:16px;
align-items:baseline}
header b{font-size:15px;letter-spacing:.5px}
header nav a{color:var(--dim);margin-right:13px;text-decoration:none}
header nav a.on{color:var(--fg)}
.wrap{display:grid;grid-template-columns:186px 1fr;gap:18px;padding:16px;
max-width:1360px;align-items:start}
.toc{position:sticky;top:56px}
.toc a{display:block;color:var(--dim);text-decoration:none;padding:3px 0;font-size:12px}
.toc a:hover{color:var(--fg)}
.toc .warnc{color:var(--warn)}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:14px 16px;margin-bottom:14px;scroll-margin-top:64px}
section>h2{margin:0 0 3px;font-size:13px;letter-spacing:.4px}
section>p.note{margin:0 0 12px;color:var(--dim);font-size:12px;max-width:78ch}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:10px}
.grid.two{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
label{display:block}
label>span.l{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.6px;margin-bottom:3px}
label>span.h{display:block;color:var(--dim);font-size:11px;margin-top:3px;
text-transform:none;letter-spacing:0}
input[type=text],input[type=email],input[type=number],input[type=date],select,textarea{
width:100%;background:#0e1014;color:var(--fg);border:1px solid var(--line);
border-radius:5px;padding:6px 8px;font:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc)}
textarea{min-height:62px;resize:vertical;line-height:1.5}
.row{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
padding:12px;margin-bottom:10px}
.rowhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}
.rowhead b{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px}
button{background:#1d2129;color:var(--fg);border:1px solid var(--line);border-radius:5px;
padding:5px 11px;font:inherit;cursor:pointer}
button:hover{border-color:var(--acc)}
button.add{color:var(--acc)}
button.del{color:var(--bad);padding:3px 8px;font-size:11px}
.bullets input{margin-bottom:5px}
.chk{display:flex;align-items:center;gap:7px}
.chk input{width:15px;height:15px;accent-color:var(--acc)}
.scr{border-bottom:1px solid var(--line);padding:11px 0}
.scr:last-child{border-bottom:none}
.scr .q{margin-bottom:6px}
.scr .q .tag{font-size:10px;color:var(--warn);border:1px solid #4a4029;
border-radius:9px;padding:0 6px;margin-left:7px;white-space:nowrap}
.scr .q .h{color:var(--dim);font-size:11px;display:block;margin-top:2px;max-width:88ch}
.opts{display:flex;flex-wrap:wrap;gap:6px}
.opts label{display:inline-flex;align-items:center;gap:5px;background:#0e1014;
border:1px solid var(--line);border-radius:14px;padding:3px 11px;cursor:pointer;
font-size:12px}
.opts label:has(input:checked){border-color:var(--acc);color:var(--fg);background:#141c26}
.opts label.unset:has(input:checked){border-color:#4a4029;background:#1d1a12;color:var(--warn)}
.opts input{accent-color:var(--acc)}
.bar{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 34%);
padding:14px 16px;display:flex;gap:12px;align-items:center;z-index:20}
.bar button.save{background:#1b3350;border-color:#26405e;color:#cfe2fb;padding:8px 20px}
.bar button.save:hover{background:#204070}
#msg{font-size:12px}
.ok{color:var(--ok)}.warnt{color:var(--warn)}.bad{color:var(--bad)}.dim{color:var(--dim)}
pre{background:#0e1014;border:1px solid var(--line);border-radius:6px;padding:10px;
white-space:pre-wrap;max-height:340px;overflow:auto;margin:8px 0 0;font-size:12px}
.banner{border:1px solid #4a4029;background:#1d1a12;color:var(--warn);
border-radius:7px;padding:10px 12px;margin-bottom:14px;font-size:12px}
.pathline{color:var(--dim);font-size:11px;margin-left:auto}
"""

JS = r"""
const $ = (t, a = {}, kids = []) => {
  const el = document.createElement(t);
  for (const [k, v] of Object.entries(a)) {
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) if (kid) el.append(kid);
  return el;
};

let dirty = false;
const touch = () => { dirty = true; msg('unsaved changes', 'warnt'); };
window.addEventListener('beforeunload', ev => { if (dirty) ev.preventDefault(); });

function msg(text, cls = 'dim') {
  const m = document.getElementById('msg');
  m.className = cls; m.textContent = text;
}

// text field bound to a path in the state object
function field(obj, key, labelText, opts = {}) {
  const id = 'f_' + Math.random().toString(36).slice(2);
  const tag = opts.type === 'textarea' ? 'textarea' : (opts.type === 'select' ? 'select' : 'input');
  const attrs = { id, placeholder: opts.placeholder || '' };
  if (tag === 'input') attrs.type = opts.type || 'text';
  if (opts.step) attrs.step = opts.step;
  const input = $(tag, attrs);
  if (tag === 'select') for (const o of opts.options || [])
    input.append($('option', { value: o }, [document.createTextNode(o || '(unset)')]));
  input.value = obj[key] === null || obj[key] === undefined ? '' : String(obj[key]);
  input.addEventListener('input', () => { obj[key] = input.value; touch(); });
  input.addEventListener('change', () => { obj[key] = input.value; touch(); });
  const lab = $('label', {}, [$('span', { class: 'l' }, [document.createTextNode(labelText)]), input]);
  if (opts.help) lab.append($('span', { class: 'h' }, [document.createTextNode(opts.help)]));
  return lab;
}

function checkbox(obj, key, labelText) {
  const input = $('input', { type: 'checkbox' });
  input.checked = !!obj[key];
  input.addEventListener('change', () => { obj[key] = input.checked; touch(); });
  return $('label', { class: 'chk' }, [input, document.createTextNode(labelText)]);
}

// a growable list of plain strings (bullets, skills, awards...)
function stringList(arr, placeholder, addLabel) {
  const box = $('div', { class: 'bullets' });
  const draw = () => {
    box.innerHTML = '';
    arr.forEach((v, i) => {
      const inp = $('input', { type: 'text', value: v, placeholder });
      inp.addEventListener('input', () => { arr[i] = inp.value; touch(); });
      const del = $('button', { class: 'del', type: 'button',
        onclick: () => { arr.splice(i, 1); touch(); draw(); } }, [document.createTextNode('remove')]);
      box.append($('div', { style: 'display:flex;gap:6px;margin-bottom:5px' }, [inp, del]));
    });
    box.append($('button', { class: 'add', type: 'button',
      onclick: () => { arr.push(''); touch(); draw(); } },
      [document.createTextNode('+ ' + addLabel)]));
  };
  draw();
  return box;
}

// a growable list of objects, each rendered by `body(item, index)`
function objectList(arr, title, blank, body) {
  const box = $('div');
  const draw = () => {
    box.innerHTML = '';
    arr.forEach((item, i) => {
      const head = $('div', { class: 'rowhead' }, [
        $('b', {}, [document.createTextNode(title + ' ' + (i + 1))]),
        $('button', { class: 'del', type: 'button',
          onclick: () => { arr.splice(i, 1); touch(); draw(); } },
          [document.createTextNode('remove')]),
      ]);
      box.append($('div', { class: 'row' }, [head, body(item, i)]));
    });
    box.append($('button', { class: 'add', type: 'button',
      onclick: () => { arr.push(structuredClone(blank)); touch(); draw(); } },
      [document.createTextNode('+ add ' + title.toLowerCase())]));
  };
  draw();
  return box;
}

function section(id, heading, note, kids) {
  return $('section', { id }, [
    $('h2', {}, [document.createTextNode(heading)]),
    note ? $('p', { class: 'note' }, [document.createTextNode(note)]) : null,
  ].concat([].concat(kids)));
}

// ---- screening: nothing is preselected; "not set" is a real answer ----
function screeningRow(spec, scr) {
  const cur = scr[spec.key];
  const q = $('div', { class: 'q' });
  q.append(document.createTextNode(spec.label));
  if (spec.legal) q.append($('span', { class: 'tag' }, [document.createTextNode('legally significant')]));
  if (spec.help) q.append($('span', { class: 'h' }, [document.createTextNode(spec.help)]));

  const wrap = $('div', { class: 'scr' }, [q]);
  const name = 'scr_' + spec.key;

  if (spec.kind === 'text') {
    const inp = $('input', { type: 'text', placeholder: spec.placeholder || '' });
    inp.value = cur === null || cur === undefined ? '' : String(cur);
    inp.addEventListener('input', () => {
      scr[spec.key] = inp.value.trim() === '' ? null : inp.value; touch(); paintToc();
    });
    wrap.append(inp);
    return wrap;
  }

  const choices = spec.kind === 'bool'
    ? [['', 'not set'], [true, 'Yes'], [false, 'No']]
    : [['', 'not set']].concat((spec.options || []).map(o => [o, o]));

  const opts = $('div', { class: 'opts' });
  for (const [val, text] of choices) {
    const input = $('input', { type: 'radio', name });
    const isUnset = val === '';
    // strict compare so `false` is distinguishable from unset
    input.checked = isUnset
      ? (cur === null || cur === undefined || cur === '')
      : cur === val;
    input.addEventListener('change', () => {
      scr[spec.key] = isUnset ? null : val; touch(); paintToc();
    });
    opts.append($('label', { class: isUnset ? 'unset' : '' },
      [input, document.createTextNode(text)]));
  }
  wrap.append(opts);
  return wrap;
}

function paintToc() {
  const unset = CORE.filter(k => {
    const v = STATE.screening[k];
    return v === null || v === undefined || v === '';
  }).length;
  const el = document.getElementById('toc-screening');
  if (!el) return;
  el.textContent = unset ? `screening (${unset} unset)` : 'screening';
  el.className = unset ? 'warnc' : '';
}

// ---- save ----
async function save() {
  msg('saving…');
  const res = await fetch('/api/profile', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collect()),
  });
  const out = await res.json();
  if (!out.ok) { msg('not saved — ' + (out.errors || []).join(' | '), 'bad'); return; }
  dirty = false;
  const m = out.missing_core || [];
  msg(`saved ${out.path} — ${out.roles} roles, ${out.years}y, `
      + `${out.screening_set} screening answers`
      + (m.length ? ` — still unset: ${m.join(', ')}` : ' — preflight clear'),
      m.length ? 'warnt' : 'ok');
  const b = document.getElementById('banner');
  if (b) b.remove();
}

function collect() {
  const s = structuredClone(STATE);
  s.skills = (s.skills || []).filter(r => (r.category || '').trim());
  s.extra = (s.extra || []).filter(r => (r.label || '').trim());
  return s;
}

async function uploadResume(file) {
  msg('reading ' + file.name + '…');
  const body = await file.arrayBuffer();
  const res = await fetch('/api/resume-text', { method: 'POST', body });
  const out = await res.json();
  const pre = document.getElementById('resumetext');
  if (!out.ok) { msg('could not read that PDF: ' + out.error, 'bad'); return; }
  pre.textContent = out.text || '(no text layer — this PDF may be a scan)';
  msg(`extracted ${out.chars} characters — copy what you recognise into the fields above`, 'ok');
}
"""


def render(form: dict[str, Any], path: str, screening_spec: list[dict[str, Any]],
           core: list[str], pref_spec: list[tuple], work_pref: list[str],
           tailnet_url: str = "", fresh: bool = False) -> bytes:
    """The whole editor as one document, with current values embedded as JSON."""
    # skills/extra are dicts on disk and lists of rows in the form
    skills_rows = [{"category": k, "items": ", ".join(v)}
                   for k, v in (form.get("skills") or {}).items()]
    extra_rows = [{"label": k, "value": v}
                  for k, v in (form.get("extra") or {}).items()]

    state = {
        "identity": {
            "first_name": "", "last_name": "", "email": "", "phone": "",
            "linkedin": "", "github": "", "website": "",
            **(form.get("identity") or {}),
            "location": {"city": "", "state": "", "country": "United States",
                         "postal_code": "", "street": "",
                         **((form.get("identity") or {}).get("location") or {})},
        },
        "headline": form.get("headline", ""),
        "summary": form.get("summary", ""),
        "target_titles": form.get("target_titles") or [],
        "target_locations": form.get("target_locations") or [],
        "target_companies": form.get("target_companies") or [],
        "exclude_companies": form.get("exclude_companies") or [],
        "remote_ok": form.get("remote_ok", True),
        "onsite_ok": form.get("onsite_ok", True),
        "hybrid_ok": form.get("hybrid_ok", True),
        "willing_to_relocate": form.get("willing_to_relocate", False),
        "min_requirement_match": form.get("min_requirement_match", 0.5),
        "experience": form.get("experience") or [],
        "education": form.get("education") or [],
        "projects": form.get("projects") or [],
        "skills": skills_rows,
        "awards": form.get("awards") or [],
        "publications": form.get("publications") or [],
        "certifications": form.get("certifications") or [],
        "languages": form.get("languages") or [],
        "compensation": {"target_base": "", "minimum_base": "", "currency": "USD",
                         **(form.get("compensation") or {})},
        "earliest_start": form.get("earliest_start", ""),
        "notice_period_weeks": form.get("notice_period_weeks", ""),
        "work_preference": form.get("work_preference", ""),
        "timeline_notes": form.get("timeline_notes", ""),
        "how_heard": form.get("how_heard", ""),
        "why_this_company_notes": form.get("why_this_company_notes", ""),
        "extra": extra_rows,
        "screening": form.get("screening") or {},
    }

    banner = ""
    if fresh:
        banner = ('<div class=banner id=banner>Nothing saved yet. Fill in what you '
                  'can and press Save — it validates and tells you what is still '
                  'missing. Every screening answer starts <b>not set</b>, and stays '
                  'that way until you choose: an unset answer stops the one '
                  'application that asks, rather than being guessed.</div>')

    boot = f"""
const STATE = {json.dumps(state)};
const SPEC = {json.dumps(screening_spec)};
const CORE = {json.dumps(core)};
const PREF = {json.dumps(pref_spec)};
const WORKPREF = {json.dumps(work_pref)};
"""

    build = r"""
const root = document.getElementById('form');
const I = STATE.identity, L = STATE.identity.location;

root.append(section('identity', 'Identity',
  'Copied verbatim into name, email, phone and link fields. The address is also '
  + 'used to answer "where will you work from".', [
  $('div', { class: 'grid' }, [
    field(I, 'first_name', 'first name'),
    field(I, 'last_name', 'last name'),
    field(I, 'email', 'email', { type: 'text', placeholder: 'you@example.com' }),
    field(I, 'phone', 'phone', { placeholder: '(555) 123-4567' }),
    field(I, 'linkedin', 'linkedin url'),
    field(I, 'github', 'github url'),
    field(I, 'website', 'personal site'),
  ]),
  $('div', { class: 'grid', style: 'margin-top:10px' }, [
    field(L, 'city', 'city'), field(L, 'state', 'state / province'),
    field(L, 'country', 'country'), field(L, 'postal_code', 'postal code'),
    field(L, 'street', 'street', { help: 'Only needed by forms that ask for a full address.' }),
  ]),
]));

root.append(section('pitch', 'Headline and summary',
  'The headline sits under your name on the resume. A summary that only restates '
  + 'your experience section costs space a bullet could use — leave it empty if so.', [
  $('div', { class: 'grid two' }, [
    field(STATE, 'headline', 'headline',
      { placeholder: 'Software Engineer — backend and data infrastructure' }),
  ]),
  $('div', { style: 'margin-top:10px' }, [
    field(STATE, 'summary', 'summary', { type: 'textarea',
      placeholder: 'One or two concrete sentences.' }),
  ]),
]));

root.append(section('targeting', 'What you are targeting',
  'Drives which postings are even attempted. Interview odds plateau around 50% of '
  + 'listed requirements, so screening yourself at 80% costs interviews — that '
  + 'is why the default threshold is 0.5.', [
  $('div', { class: 'grid two' }, [
    $('div', {}, [$('span', { class: 'l' }, [document.createTextNode('target titles')]),
      stringList(STATE.target_titles, 'Software Engineer', 'add title')]),
    $('div', {}, [$('span', { class: 'l' }, [document.createTextNode('target locations')]),
      stringList(STATE.target_locations, 'San Francisco / Remote', 'add location')]),
    $('div', {}, [$('span', { class: 'l' },
      [document.createTextNode('target companies (optional)')]),
      stringList(STATE.target_companies, 'Anthropic', 'add company')]),
    $('div', {}, [$('span', { class: 'l' },
      [document.createTextNode('never apply to')]),
      stringList(STATE.exclude_companies, 'Palantir', 'add company'),
      $('span', { class: 'h' }, [document.createTextNode(
        'Already applied by hand, current employer, anywhere a second application must not land. Never queued.')])]),
  ]),
  $('div', { class: 'grid', style: 'margin-top:12px' }, [
    checkbox(STATE, 'remote_ok', 'open to remote'),
    checkbox(STATE, 'onsite_ok', 'open to onsite'),
    checkbox(STATE, 'hybrid_ok', 'open to hybrid'),
    checkbox(STATE, 'willing_to_relocate', 'willing to relocate'),
  ]),
  $('div', { class: 'grid', style: 'margin-top:12px' }, [
    field(STATE, 'min_requirement_match', 'minimum requirement match',
      { type: 'number', step: '0.05', help: '0.5 = apply at half the listed requirements.' }),
  ]),
]));

root.append(section('experience', 'Experience',
  'Keep your real numbers in the bullets. Tailoring may rephrase them; it may '
  + 'never invent a metric that is not already here, and the fabrication check '
  + 'blocks the application if it does.',
  objectList(STATE.experience, 'Role',
    { company: '', title: '', start: '', end: '', location: '', tech: [], bullets: [], gap_explanation: '' },
    (item) => $('div', {}, [
      $('div', { class: 'grid' }, [
        field(item, 'company', 'company'),
        field(item, 'title', 'title'),
        field(item, 'start', 'start', { type: 'date' }),
        field(item, 'end', 'end', { type: 'date', help: 'Leave empty if this is current.' }),
        field(item, 'location', 'location', { placeholder: 'San Francisco, CA' }),
      ]),
      $('div', { style: 'margin-top:10px' }, [
        $('span', { class: 'l' }, [document.createTextNode('tech used')]),
        stringList(item.tech, 'Python', 'add tech'),
      ]),
      $('div', { style: 'margin-top:10px' }, [
        $('span', { class: 'l' }, [document.createTextNode('bullets — what you did, with numbers')]),
        stringList(item.bullets, 'Cut p99 write latency from 840ms to 95ms by rebuilding the ingestion path.', 'add bullet'),
      ]),
      $('div', { style: 'margin-top:10px' }, [
        field(item, 'gap_explanation', 'gap explanation (if a >6mo gap precedes this role)',
          { type: 'textarea', help: 'About half of employers auto-screen unexplained gaps over six months. Explaining one measured 6.8% vs 4.3% callbacks in a 36,510-opening field experiment.' }),
      ]),
    ]))));

root.append(section('education', 'Education',
  'An expected end date is fine. Leave "completed" off while you are still enrolled '
  + '— the resume then prints it as expected.',
  objectList(STATE.education, 'School',
    { school: '', degree: '', field_of_study: '', start: '', end: '', gpa: '', completed: false },
    (item) => $('div', {}, [
      $('div', { class: 'grid' }, [
        field(item, 'school', 'school'),
        field(item, 'degree', 'degree', { placeholder: 'B.S.' }),
        field(item, 'field_of_study', 'field of study', { placeholder: 'Computer Science' }),
        field(item, 'start', 'start', { type: 'date' }),
        field(item, 'end', 'end', { type: 'date' }),
        field(item, 'gpa', 'gpa', { type: 'number', step: '0.01',
          help: 'Leave empty to skip GPA questions entirely.' }),
      ]),
      $('div', { style: 'margin-top:9px' }, [checkbox(item, 'completed', 'degree completed')]),
    ]))));

root.append(section('projects', 'Projects',
  'A link a reviewer can open is worth more than a description. These also feed '
  + '"tell us about a project" answers.',
  objectList(STATE.projects, 'Project',
    { name: '', description: '', url: '', repo: '', tech: [], bullets: [] },
    (item) => $('div', {}, [
      $('div', { class: 'grid' }, [
        field(item, 'name', 'name'),
        field(item, 'url', 'link'),
        field(item, 'repo', 'repo (if different)'),
      ]),
      $('div', { style: 'margin-top:10px' }, [
        field(item, 'description', 'one line on what it does'),
      ]),
      $('div', { style: 'margin-top:10px' }, [
        $('span', { class: 'l' }, [document.createTextNode('tech')]),
        stringList(item.tech, 'FastAPI', 'add tech'),
      ]),
      $('div', { style: 'margin-top:10px' }, [
        $('span', { class: 'l' }, [document.createTextNode('bullets')]),
        stringList(item.bullets, 'What you built and one real constraint you designed around.', 'add bullet'),
      ]),
    ]))));

root.append(section('skills', 'Skills',
  'Grouped how you want them printed. A skill the profile does not list gets '
  + 'stripped out of a generated resume, so anything you genuinely have belongs here.',
  objectList(STATE.skills, 'Category', { category: '', items: '' },
    (item) => $('div', { class: 'grid two' }, [
      field(item, 'category', 'category', { placeholder: 'Languages' }),
      field(item, 'items', 'comma-separated', { placeholder: 'Python, Go, TypeScript, SQL' }),
    ]))));

root.append(section('credentials', 'Awards, publications, certifications, languages',
  'Peer-reviewed work, competition wins and selective programs are expensive to '
  + 'fake and cheap to verify, which is why they still carry weight when generated '
  + 'prose no longer does.', [
  $('div', { class: 'grid two' }, [
    $('div', {}, [$('span', { class: 'l' }, [document.createTextNode('awards & selective programs')]),
      stringList(STATE.awards, 'ICPC Regional — 2nd place, 2025', 'add award')]),
    $('div', {}, [$('span', { class: 'l' }, [document.createTextNode('publications')]),
      stringList(STATE.publications, 'Author, "Title", Venue 2025', 'add publication')]),
    $('div', {}, [$('span', { class: 'l' }, [document.createTextNode('certifications')]),
      stringList(STATE.certifications, 'AWS Solutions Architect Associate', 'add certification')]),
    $('div', {}, [$('span', { class: 'l' }, [document.createTextNode('languages spoken')]),
      stringList(STATE.languages, 'English (native)', 'add language')]),
  ]),
]));

root.append(section('comp', 'Compensation',
  'Where a posting publishes a band, the answer anchors inside its upper half. '
  + 'Where it does not, your target becomes the bottom of a range about 15% wide. '
  + 'Leave both empty and salary fields are composed instead.', [
  $('div', { class: 'grid' }, [
    field(STATE.compensation, 'target_base', 'target base', { placeholder: '150000' }),
    field(STATE.compensation, 'minimum_base', 'minimum acceptable', { placeholder: '130000' }),
    field(STATE.compensation, 'currency', 'currency'),
  ]),
]));

const prefKids = [$('div', { class: 'grid two' }, PREF.map(([key, label, type, ph, help]) =>
  field(STATE, key, label, {
    type: type, placeholder: ph, help: help,
    options: type === 'select' ? WORKPREF : null,
  })))];
root.append(section('prefs', 'Preferences forms ask constantly',
  'Not legally significant, but without these the model correctly refuses to answer '
  + '— which leaves required fields empty and blocks the whole application.',
  prefKids));

root.append(section('extra', 'Anything else — free-form',
  'Label and value. Every application invents its own questions, so no fixed set of '
  + 'fields covers them; whatever you put here reaches the model and can be drawn on '
  + 'for a non-legal answer. Good candidates: security clearance details, portfolio '
  + 'passwords, publication links, conference talks, teaching, open-source '
  + 'maintainership, hackathon wins, availability quirks, pronouns, preferred name, '
  + 'accommodations you want noted, why you left a role, a one-line answer to "tell '
  + 'me about yourself". Screening answers do NOT go here — those only count '
  + 'from the section below, where provenance is tracked.',
  objectList(STATE.extra, 'Fact', { label: '', value: '' },
    (item) => $('div', { class: 'grid two' }, [
      field(item, 'label', 'label', { placeholder: 'Open-source maintainership' }),
      field(item, 'value', 'value', { type: 'textarea',
        placeholder: 'Maintainer of X (2.1k stars); merged 40 PRs from contributors in 2025.' }),
    ]))));

const scrBox = $('div');
for (const spec of SPEC) scrBox.append(screeningRow(spec, STATE.screening));
root.append(section('screening', 'Screening answers',
  'These are statements you make under your own name, often under an explicit '
  + 'attestation of truthfulness. Each one is answered ONLY from what you set here '
  + '— never inferred, never defaulted to "yes", never chosen by the model. '
  + 'Leave one "not set" and the single application that asks it stops and tells '
  + 'you; leave a core one unset and an autonomous run refuses to start at all.',
  [scrBox]));

root.append(section('resume', 'Have a resume PDF?',
  'This extracts the text so you can copy your real bullets and numbers across. It '
  + 'deliberately does not auto-fill: parsing a resume into structured claims is '
  + 'exactly where a plausible wrong employer or date gets introduced, and this file '
  + 'is the thing that is supposed to be true.', [
  $('input', { type: 'file', accept: '.pdf',
    onchange: (ev) => ev.target.files[0] && uploadResume(ev.target.files[0]) }),
  $('pre', { id: 'resumetext' }, [document.createTextNode('')]),
]));

paintToc();
"""

    toc = [("identity", "identity"), ("pitch", "headline"), ("targeting", "targeting"),
           ("experience", "experience"), ("education", "education"),
           ("projects", "projects"), ("skills", "skills"),
           ("credentials", "credentials"), ("comp", "compensation"),
           ("prefs", "preferences"), ("extra", "free-form"),
           ("screening", "screening"), ("resume", "resume pdf")]
    toc_html = "".join(
        f'<a href="#{i}"'
        + (f' id="toc-{i}"' if i == "screening" else "")
        + f">{n}</a>"
        for i, n in toc)

    nav = ('<a href="/">overview</a><a href="/answers">answers</a>'
           '<a href="/profile">profile</a><a href="/edit" class=on>edit profile</a>'
           '<a href="/status">status</a>')

    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>jobbot — edit profile</title>
<style>{CSS}</style></head><body>
<header><b>jobbot</b><nav>{nav}</nav>
<span class=pathline>{html.escape(path)}{
  ' &middot; ' + html.escape(tailnet_url) if tailnet_url else ''}</span></header>
<div class=wrap>
  <div class=toc>{toc_html}</div>
  <div>{banner}<div id=form></div>
    <div class=bar>
      <button class=save type=button onclick="save()">Save profile</button>
      <span id=msg class=dim>loaded</span>
    </div>
  </div>
</div>
<script>{JS}
{boot}
{build}
</script></body></html>""".encode()
