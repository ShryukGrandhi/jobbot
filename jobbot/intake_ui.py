"""The intake wizard: dump everything in, review what the model made of it.

Motion notes, since it is deliberate rather than decorative:

  This is a rare, first-time flow -- you fill your profile once -- which is the
  only tier where a delight budget is justified. Every animation here still
  names a purpose:

    step change      spatial consistency (where you came from, where you went)
    processing       state indication (the model is working; don't touch it)
    card reveal      preventing a jarring change (twenty cards teleporting in)
    accept / skip    feedback (the click registered)
    mic ring         state indication -- and a privacy signal: it must be
                     obvious the microphone is live

  transform and opacity only, so nothing triggers layout. Transitions for
  anything clickable twice in a second (accept, skip, step nav) because they
  retarget from the current value; CSS animations only for the processing state
  and the reveal, which are predetermined and better off the main thread while
  the page is parsing a large JSON response.
"""

from __future__ import annotations

import json
from typing import Any

CSS = """
:root{
  --bg:#0b0c0f;--panel:#13151a;--panel2:#181b21;--line:#23262e;--line2:#2e323c;
  --fg:#e2e5ea;--dim:#7d838f;--dim2:#9aa1ad;
  --acc:#6aa9f0;--acc-dim:#26405e;--ok:#5ec27a;--ok-dim:#2c4a35;
  --warn:#e0b341;--warn-dim:#4a4029;--bad:#e0605e;
  --ease-out:cubic-bezier(0.23,1,0.32,1);
  --ease-in-out:cubic-bezier(0.77,0,0.175,1);
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);
font:13.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
a{color:var(--acc)}
::selection{background:var(--acc-dim)}

header{position:sticky;top:0;z-index:30;background:rgba(11,12,15,.86);
backdrop-filter:blur(12px);border-bottom:1px solid var(--line);
padding:11px 20px;display:flex;gap:18px;align-items:center}
header b{font-size:15px;letter-spacing:.5px}
header nav a{color:var(--dim);margin-right:13px;text-decoration:none;
transition:color 140ms var(--ease-out)}
header nav a:hover{color:var(--fg)}
header nav a.on{color:var(--fg)}

/* ---- step rail ---- */
.rail{display:flex;gap:7px;align-items:center;margin-left:auto}
.rail i{width:26px;height:3px;border-radius:2px;background:var(--line2);
transition:background 200ms var(--ease-out),transform 200ms var(--ease-out);
transform-origin:left}
.rail i.done{background:var(--ok-dim)}
.rail i.now{background:var(--acc);transform:scaleX(1.12)}

main{max-width:940px;margin:0 auto;padding:26px 20px 150px}

/* ---- steps: only one mounted at a time, so a transition is enough ---- */
.step{display:none}
.step.live{display:block}
.step.live>*{animation:rise 260ms var(--ease-out) both}
@keyframes rise{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}

h1{font-size:22px;margin:0 0 6px;letter-spacing:-.2px}
.sub{color:var(--dim2);margin:0 0 22px;max-width:74ch}

.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;
padding:16px 18px;margin-bottom:14px}
.card>h3{margin:0 0 3px;font-size:13.5px;letter-spacing:.2px}
.card>p.hint{margin:0 0 13px;color:var(--dim);font-size:12.5px;max-width:80ch}

label span.l{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.7px;margin-bottom:4px}
input[type=text],textarea{width:100%;background:#0d0f13;color:var(--fg);
border:1px solid var(--line);border-radius:7px;padding:9px 11px;font:inherit;
transition:border-color 140ms var(--ease-out),box-shadow 140ms var(--ease-out)}
input:focus,textarea:focus{outline:none;border-color:var(--acc);
box-shadow:0 0 0 3px rgba(106,169,240,.11)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:11px}

button{background:#1c2028;color:var(--fg);border:1px solid var(--line2);
border-radius:7px;padding:7px 13px;font:inherit;cursor:pointer;
transition:background 140ms var(--ease-out),border-color 140ms var(--ease-out),
transform 100ms var(--ease-out),opacity 140ms var(--ease-out)}
button:hover:not(:disabled){border-color:var(--acc)}
button:active:not(:disabled){transform:scale(.975)}
button:disabled{opacity:.42;cursor:not-allowed}
button.primary{background:#1b3350;border-color:var(--acc-dim);color:#d3e4fb;
padding:10px 20px;font-weight:500}
button.primary:hover:not(:disabled){background:#204572}
button.ghost{background:transparent;border-color:var(--line)}
button.tiny{padding:3px 9px;font-size:11.5px}

/* ---- dictation ---- */
.dictate{position:relative}
.dictate textarea{min-height:190px;resize:vertical;line-height:1.65}
.microw{display:flex;gap:10px;align-items:center;margin-bottom:9px;flex-wrap:wrap}
.mic{display:inline-flex;gap:8px;align-items:center;position:relative}
.mic svg{flex:none}
.mic.live{background:#3a1c1f;border-color:#6d2b2b;color:#ff9d9b}
/* Ring is the privacy signal: while the mic is open it must be unmissable.
   Constant motion, so linear; slow, so it reads as "ongoing" not "loading". */
.mic.live::after{content:'';position:absolute;inset:-4px;border-radius:9px;
border:1.5px solid #6d2b2b;animation:ring 1400ms linear infinite;pointer-events:none}
@keyframes ring{0%{opacity:.85;transform:scale(.98)}100%{opacity:0;transform:scale(1.12)}}
.interim{color:var(--dim);font-style:italic}
.count{position:absolute;bottom:8px;right:26px;color:var(--dim);font-size:11px;
pointer-events:none;background:#0d0f13;padding:0 4px;border-radius:4px}

/* ---- drop zone ---- */
.drop{border:1.5px dashed var(--line2);border-radius:10px;padding:24px;
text-align:center;color:var(--dim);cursor:pointer;
transition:border-color 140ms var(--ease-out),background 140ms var(--ease-out),
transform 140ms var(--ease-out)}
.drop:hover{border-color:var(--line2);background:#15181e}
.drop.over{border-color:var(--acc);background:#141c26;color:var(--fg);
transform:scale(1.006)}
.files{display:flex;flex-direction:column;gap:8px;margin-top:11px;align-items:stretch}
.files>button{align-self:flex-start}
.file{display:flex;gap:10px;align-items:center;background:var(--panel2);
border:1px solid var(--line);border-radius:8px;padding:9px 11px;
animation:rise 220ms var(--ease-out) both}
.file .nm{font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
max-width:190px}
.file .ok{color:var(--ok);font-size:11px;white-space:nowrap}
.file .bad{color:var(--bad);font-size:11px}
.file input{flex:1;min-width:120px;padding:5px 8px;font-size:12px}

/* ---- processing ---- */
.working{padding:52px 0;text-align:center}
.scan{height:2px;width:230px;margin:0 auto 24px;border-radius:2px;
background:var(--line2);overflow:hidden;position:relative}
.scan::after{content:'';position:absolute;inset:0;border-radius:2px;
background:linear-gradient(90deg,transparent,var(--acc),transparent);
animation:scan 1250ms var(--ease-in-out) infinite}
@keyframes scan{from{transform:translateX(-100%)}to{transform:translateX(100%)}}
.working .what{color:var(--fg);margin-bottom:5px}
.working .who{color:var(--dim);font-size:12px}
.tick{color:var(--dim);font-size:12.5px;margin-top:5px;
animation:rise 220ms var(--ease-out) both}
.tick.on{color:var(--ok)}
.elapsed{color:var(--line2);font-size:11.5px;margin-top:16px;
font-variant-numeric:tabular-nums}

/* ---- review cards ---- */
.group{margin-bottom:26px}
.group>h2{font-size:11px;text-transform:uppercase;letter-spacing:1.1px;
color:var(--dim);margin:0 0 9px;display:flex;align-items:center;gap:10px}
.group>h2 .n{color:var(--dim2);font-weight:400}
.bulk{display:flex;gap:9px;align-items:center;background:var(--panel);
border:1px solid var(--line);border-radius:10px;padding:11px 14px;margin-bottom:18px;
flex-wrap:wrap}
.bulk .t{color:var(--dim2);font-size:12.5px;flex:1;min-width:180px}
.saved{border:1px solid var(--ok-dim);background:#111a13;border-radius:10px;
padding:13px 15px;margin-bottom:18px;font-size:12.5px;color:#bfe0c9}
.saved .left{margin-top:8px;color:var(--dim2)}
.failed{border:1px solid #5c2b2b;background:#1c1213;border-radius:10px;
padding:13px 15px;margin-bottom:18px;color:#f0b3b1;font-size:12.5px}
.failed ul{margin:7px 0 0;padding-left:19px}
.prop{background:var(--panel);border:1px solid var(--line);border-left:2px solid var(--line2);
border-radius:9px;padding:13px 15px;margin-bottom:9px;
animation:rise 220ms var(--ease-out) both;
animation-delay:calc(var(--i) * 40ms);
transition:border-left-color 160ms var(--ease-out),opacity 160ms var(--ease-out),
transform 160ms var(--ease-out),background 160ms var(--ease-out)}
.prop.yes{border-left-color:var(--ok);background:#141a15}
.prop.no{opacity:.34;transform:scale(.994)}
.prop .top{display:flex;gap:12px;align-items:baseline;margin-bottom:7px}
.prop .t{font-weight:500;flex:1}
.prop .src{color:var(--dim);font-size:11px;white-space:nowrap}
.prop .val{color:var(--dim2);font-size:12.5px;white-space:pre-wrap}
.prop .val ul{margin:5px 0 0;padding-left:17px}
.prop .val .k{color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.6px;margin-right:7px}
.prop .val li{margin-bottom:2px}
.prop .was{color:var(--dim);font-size:11.5px;margin-top:6px;
padding-top:6px;border-top:1px solid var(--line)}
.prop .acts{display:flex;gap:7px;margin-top:10px}
.prop .acts button.on{border-color:var(--ok-dim);background:#16261b;color:#9fe0b4}

.note{border:1px solid var(--warn-dim);background:#191710;border-radius:9px;
padding:11px 14px;margin-bottom:8px;font-size:12.5px;color:#e8d9b0;
animation:rise 220ms var(--ease-out) both;animation-delay:calc(var(--i) * 40ms)}
.note.info{border-color:var(--line);background:var(--panel);color:var(--dim2)}
.note.info .k{color:var(--dim)}
.note .k{color:var(--warn);font-size:10.5px;text-transform:uppercase;
letter-spacing:.8px;margin-right:8px}

/* ---- interview ---- */
.q{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin-bottom:11px;animation:rise 240ms var(--ease-out) both;
animation-delay:calc(var(--i) * 50ms)}
.q .qt{margin-bottom:3px}
.q .why{color:var(--dim);font-size:12px;margin-bottom:9px}
.q textarea{min-height:74px;resize:vertical}
.q.answered{border-color:var(--ok-dim)}

/* ---- sticky action bar ---- */
/* The bar is a fixed full-width band. Without pointer-events:none its gradient
   swallowed every click in the bottom ~84px of the viewport -- including the
   mic button, which was therefore impossible to press. Only the controls
   themselves should be clickable. */
.actions{position:fixed;left:0;right:0;bottom:0;z-index:30;pointer-events:none;
background:linear-gradient(transparent,rgba(11,12,15,.94) 42%);
padding:22px 20px 18px;display:flex;justify-content:center}
.actions .inner{width:100%;max-width:940px;display:flex;gap:13px;align-items:center;
pointer-events:none}
.actions button{pointer-events:auto}
.msg{font-size:12.5px;color:var(--dim);min-width:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.msg.ok{color:var(--ok)}.msg.bad{color:var(--bad)}.msg.warn{color:var(--warn)}
.spacer{flex:1}

/* ---- narrow screens: the action bar was wrapping every label onto three
   lines, which made the primary action the hardest thing to hit ---- */
@media (max-width:620px){
  main{padding:18px 14px 150px}
  h1{font-size:19px}
  header{padding:9px 14px;flex-wrap:wrap;gap:8px}
  header nav a{margin-right:10px;font-size:12px}
  .rail{display:none}
  .card{padding:14px}
  .actions{padding:16px 14px 14px}
  .actions .inner{flex-wrap:wrap;gap:9px}
  .actions .msg{order:-1;width:100%;text-align:center}
  .actions .spacer{display:none}
  .actions button{flex:1;white-space:nowrap;padding:10px 12px}
  .actions button.primary{flex:2}
  .file{flex-wrap:wrap}
  .file .nm{max-width:100%}
  .prop .top{flex-wrap:wrap;gap:4px}
}

@media (prefers-reduced-motion:reduce){
  /* Keep the fades -- they carry meaning -- drop everything that moves. */
  .step.live>*,.file,.prop,.note,.q{animation:fade 180ms ease both}
  @keyframes fade{from{opacity:0}to{opacity:1}}
  .scan::after{animation:pulse 1400ms ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:.25}50%{opacity:1}}
  .mic.live::after{animation:pulse 1400ms ease-in-out infinite}
  .rail i.now{transform:none}
  button:active:not(:disabled){transform:none}
  .prop.no{transform:none}
  .drop.over{transform:none}
}
@media (hover:hover) and (pointer:fine){
  .prop:hover{background:#161920}
  .prop.yes:hover{background:#16261b}
}
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
const txt = s => document.createTextNode(s == null ? '' : String(s));
const byId = id => document.getElementById(id);

const S = {
  step: 'sources',
  links: { linkedin: '', github: '', website: '' },
  extraLinks: [],
  files: [],          // {name, label, text, chars, error}
  dump: '',
  cards: [], notes: [], questions: [], saved: null,
  msg: { text: '', cls: '' },
  decisions: {},      // cardId -> true (accept) | false (skip)
  answers: {},        // question index -> answer text
  rounds: 0,          // how many organize passes have run
  moreQuestions: [],  // held back rather than pushed at you again
};

function msg(text, cls = '') {
  // Held in state, not only in the node: drawActions() rebuilds the bar, and
  // writing straight to the DOM meant a save confirmation vanished instantly.
  S.msg = { text, cls };
  const m = byId('msg');
  if (m) { m.className = 'msg ' + cls; m.textContent = text; }
}

const STEPS = ['sources', 'working', 'review', 'interview'];
function go(step) {
  if (step !== S.step) S.msg = { text: '', cls: '' };
  S.step = step;
  for (const s of STEPS) byId('step-' + s).classList.toggle('live', s === step);
  const order = { sources: 0, working: 1, review: 2, interview: 2 };
  [...document.querySelectorAll('.rail i')].forEach((el, i) => {
    el.classList.toggle('done', i < order[step]);
    el.classList.toggle('now', i === order[step]);
  });
  window.scrollTo({ top: 0, behavior: 'instant' });
  drawActions();
}

/* ---------------- step 1: sources ---------------- */
function drawSources() {
  const root = byId('step-sources');
  root.innerHTML = '';

  root.append($('h1', {}, txt('Dump everything you have')));
  root.append($('p', { class: 'sub' }, txt(
    'Links, however many resumes you have tailored for different roles, and '
    + 'anything you want to say out loud. Nothing here has to be tidy — the '
    + 'point of the next step is that it does not have to be.')));

  // links
  const linkCard = $('div', { class: 'card' }, [
    $('h3', {}, txt('Links')),
    $('p', { class: 'hint' }, txt(
      'These go on the resume verbatim. Nothing is fetched from them — GitHub '
      + 'and LinkedIn both block automated reading, and a scraped bio is exactly '
      + 'the kind of fact this system will not assert for you. Paste what you '
      + 'want said; describe the rest below.')),
  ]);
  const lg = $('div', { class: 'grid' });
  for (const [k, ph] of [['linkedin', 'https://linkedin.com/in/you'],
                         ['github', 'https://github.com/you'],
                         ['website', 'https://you.dev']]) {
    const i = $('input', { type: 'text', placeholder: ph, value: S.links[k] });
    i.addEventListener('input', () => { S.links[k] = i.value; drawActions(); });
    lg.append($('label', {}, [$('span', { class: 'l' }, txt(k)), i]));
  }
  linkCard.append(lg);
  const more = $('div', { class: 'files' });
  const drawMore = () => {
    more.innerHTML = '';
    S.extraLinks.forEach((row, idx) => {
      const lab = $('input', { type: 'text', placeholder: 'label (e.g. Devpost, blog, paper)', value: row.label });
      const url = $('input', { type: 'text', placeholder: 'https://…', value: row.url });
      lab.addEventListener('input', () => { row.label = lab.value; });
      url.addEventListener('input', () => { row.url = url.value; drawActions(); });
      more.append($('div', { class: 'file' }, [lab, url,
        $('button', { class: 'tiny ghost', onclick: () => { S.extraLinks.splice(idx, 1); drawMore(); drawActions(); } }, txt('remove'))]));
    });
    more.append($('button', { class: 'tiny ghost', onclick: () => { S.extraLinks.push({ label: '', url: '' }); drawMore(); } }, txt('+ another link')));
  };
  drawMore();
  linkCard.append(more);
  root.append(linkCard);

  // resumes
  const dropCard = $('div', { class: 'card' }, [
    $('h3', {}, txt('Resumes')),
    $('p', { class: 'hint' }, txt(
      'Drop as many as you like — the ML one, the backend one, the old one. '
      + 'Label each so the model knows which role it was aimed at. Several '
      + 'resumes describe one career, so they get merged per role rather than '
      + 'duplicated, and anything they disagree on is flagged instead of picked.')),
  ]);
  const drop = $('div', { class: 'drop' }, txt('Drop PDFs here, or click to choose'));
  const picker = $('input', { type: 'file', accept: '.pdf', multiple: 'true', style: 'display:none' });
  drop.addEventListener('click', () => picker.click());
  picker.addEventListener('change', () => addFiles([...picker.files]));
  drop.addEventListener('dragover', ev => { ev.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', ev => {
    ev.preventDefault(); drop.classList.remove('over');
    addFiles([...ev.dataTransfer.files].filter(f => /\.pdf$/i.test(f.name)));
  });
  dropCard.append(drop, picker, $('div', { class: 'files', id: 'filelist' }));
  root.append(dropCard);

  // dictation
  const dictCard = $('div', { class: 'card' }, [
    $('h3', {}, txt('Say everything else')),
    $('p', { class: 'hint' }, txt(
      'One long paragraph is fine. Ramble. What you have built, what the numbers '
      + 'actually were, what you want next, what you would rather not do, why you '
      + 'left, what you are proud of, anything an application might ask. The next '
      + 'step cleans up the filler and keeps the facts — it will not add any.')),
  ]);
  const wrap = $('div', { class: 'dictate' });
  const ta = $('textarea', { id: 'dump', placeholder:
    "e.g. So I'm a backend engineer, been at Example Corp about two years, the "
    + "big thing I did was rebuild the ingestion path and that took p99 from "
    + "840ms down to 95ms, also migrated 38 services with no downtime. I want "
    + "inference or infra roles, San Francisco or remote, can start two weeks "
    + "after an offer…" });
  ta.value = S.dump;
  ta.addEventListener('input', () => { S.dump = ta.value; byId('cc').textContent = S.dump.length ? S.dump.length + ' chars' : ''; drawActions(); });
  const mic = $('button', { class: 'mic', id: 'mic', title: 'dictate (speech to text)' });
  mic.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    + '<rect x="9" y="2" width="6" height="11" rx="3"/>'
    + '<path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>'
    + '<span id="miclabel">Dictate</span>';
  mic.addEventListener('click', toggleMic);
  dictCard.append($('div', { class: 'microw' }, [
    mic,
    $('span', { id: 'micnote', style: 'color:var(--dim);font-size:12px' }, txt('')),
  ]));
  wrap.append(ta, $('span', { class: 'count', id: 'cc' },
    txt(S.dump.length ? S.dump.length + ' chars' : '')));
  dictCard.append(wrap);
  root.append(dictCard);
  drawFiles();
}

function addFiles(list) {
  for (const f of list) {
    const rec = { name: f.name, label: '', text: '', chars: 0, error: '', busy: true };
    S.files.push(rec);
    drawFiles();
    f.arrayBuffer().then(async buf => {
      const res = await fetch('/api/resume-text', { method: 'POST', body: buf });
      const out = await res.json();
      rec.busy = false;
      if (out.ok) { rec.text = out.text; rec.chars = out.chars; }
      else rec.error = out.error || 'could not read';
      drawFiles(); drawActions();
    });
  }
}

function drawFiles() {
  const box = byId('filelist');
  if (!box) return;
  box.innerHTML = '';
  S.files.forEach((f, i) => {
    const lab = $('input', { type: 'text', value: f.label,
      placeholder: 'what was this one tailored for? (e.g. ML infra roles)' });
    lab.addEventListener('input', () => { f.label = lab.value; });
    const state = f.busy ? $('span', { class: 'ok' }, txt('reading…'))
      : f.error ? $('span', { class: 'bad' }, txt(f.error.slice(0, 40)))
      : $('span', { class: 'ok' }, txt(f.chars.toLocaleString() + ' chars'));
    box.append($('div', { class: 'file' }, [
      $('span', { class: 'nm' }, txt(f.name)), state, lab,
      $('button', { class: 'tiny ghost', onclick: () => { S.files.splice(i, 1); drawFiles(); drawActions(); } }, txt('remove')),
    ]));
  });
}

/* ---------------- dictation via the browser's own recogniser ---------------- */
let rec = null, recOn = false, baseText = '';
function toggleMic() {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  const note = byId('micnote');
  if (!Rec) {
    note.textContent = window.isSecureContext
      ? 'This browser has no speech recognition — Chrome or Safari do. Typing works the same.'
      : 'Dictation needs a secure context. Open the https:// tailnet address and the mic works.';
    return;
  }
  if (recOn) { rec.stop(); return; }
  rec = new Rec();
  rec.continuous = true; rec.interimResults = true; rec.lang = 'en-US';
  baseText = byId('dump').value;
  rec.onstart = () => {
    recOn = true;
    byId('mic').classList.add('live');
    byId('miclabel').textContent = 'Stop';
    note.textContent = 'listening — speak normally, it keeps up';
  };
  rec.onerror = ev => { note.textContent = 'mic error: ' + ev.error; };
  rec.onend = () => {
    recOn = false;
    byId('mic').classList.remove('live');
    byId('miclabel').textContent = 'Dictate';
    note.textContent = '';
  };
  rec.onresult = ev => {
    let done = '', interim = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) done += r[0].transcript;
      else interim += r[0].transcript;
    }
    if (done) baseText = (baseText + ' ' + done.trim()).trim();
    const ta = byId('dump');
    ta.value = (baseText + (interim ? ' ' + interim : '')).trim();
    S.dump = baseText;
    byId('cc').textContent = ta.value.length + ' chars';
    ta.scrollTop = ta.scrollHeight;
    drawActions();
  };
  rec.start();
}

/* ---------------- step 2: working ---------------- */
const TICKS = [
  'reading what you gave it',
  'merging roles across resumes',
  'keeping your numbers exactly as written',
  'writing down anything it had to interpret',
  'leaving screening answers alone — those are yours',
];
let tickTimer = null, elapsedTimer = null;
function startWorking(what) {
  go('working');
  const root = byId('step-working');
  root.innerHTML = '';
  root.append($('div', { class: 'working' }, [
    $('div', { class: 'scan' }),
    $('div', { class: 'what' }, txt(what)),
    $('div', { class: 'who', id: 'ticks' }),
    $('div', { class: 'elapsed', id: 'elapsed' }),
  ]));
  let i = 0;
  const next = () => {
    if (i >= TICKS.length) return;
    byId('ticks').append($('div', { class: 'tick' }, txt(TICKS[i++])));
    tickTimer = setTimeout(next, 1600);
  };
  next();
  // The call takes 15-30s. Once the ticks run out a static screen reads as
  // hung, so keep a number moving -- it is information, not decoration.
  const t0 = Date.now();
  elapsedTimer = setInterval(() => {
    const el = byId('elapsed');
    if (el) el.textContent = Math.round((Date.now() - t0) / 1000) + 's';
  }, 1000);
}
function stopWorking() { clearTimeout(tickTimer); clearInterval(elapsedTimer); }

/* ---------------- step 3: review ---------------- */
const GROUPS = [
  ['identity', 'Contact details'], ['headline', 'Headline'], ['summary', 'Summary'],
  ['experience', 'Experience'], ['education', 'Education'], ['project', 'Projects'],
  ['skills', 'Skills'], ['list', 'Lists'], ['compensation', 'Compensation'],
  ['preference', 'Preferences'], ['extra', 'Free-form facts'],
];

function renderValue(card) {
  const b = card.body;
  if (Array.isArray(b)) {
    return $('div', { class: 'val' }, [$('ul', {}, b.map(x => $('li', {}, txt(
      typeof x === 'string' ? x : JSON.stringify(x)))))]);
  }
  if (b && typeof b === 'object') {
    const bits = [];
    const nice = k => k.replace(/_/g, ' ');
    for (const [k, v] of Object.entries(b)) {
      // `source` is shown in the card header; `name`/`label` are the card title
      // already, so repeating them is noise on the thing you are reading.
      if (k === 'source' || v === '' || v == null) continue;
      // Anything already spelled out in the card heading is noise in the body:
      // "Backend Engineer — Example Corp" does not need COMPANY and TITLE rows.
      const sv = String(v).trim();
      if (sv.length > 1 && sv.length < 70 && String(card.title).includes(sv)) continue;
      if (Array.isArray(v)) {
        if (!v.length) continue;
        bits.push($('div', {}, [$('span', { class: 'k' }, txt(nice(k))),
          $('ul', {}, v.map(x => $('li', {}, txt(x))))]));
      } else {
        bits.push($('div', {}, [$('span', { class: 'k' }, txt(nice(k) + ' ')), txt(v)]));
      }
    }
    return $('div', { class: 'val' }, bits);
  }
  return $('div', { class: 'val' }, txt(b));
}

/* Decisions are applied to the one card that was clicked. Re-rendering the
   whole list on every click destroyed and rebuilt every node, which restarted
   the staggered entrance animation on all of them -- so a click looked like a
   page flicker rather than a change of state. */
function setDecision(id, val) {
  S.decisions[id] = val;
  const el = document.querySelector(`.prop[data-id="${id}"]`);
  if (el) {
    el.classList.toggle('yes', val === true);
    el.classList.toggle('no', val === false);
    el.querySelectorAll('.acts button').forEach(b => {
      b.classList.toggle('on', (b.dataset.act === 'accept') === (val === true));
    });
  }
  refreshCounts();
}

function decideMany(ids, val) {
  for (const id of ids) setDecision(id, val);
}

function refreshCounts() {
  for (const h of document.querySelectorAll('.group[data-kind]')) {
    const kind = h.dataset.kind;
    const mine = S.cards.filter(c => c.kind === kind);
    const yes = mine.filter(c => S.decisions[c.id] === true).length;
    const label = h.querySelector('h2 .n');
    if (label) label.textContent = `· ${yes}/${mine.length}`;
  }
  const total = S.cards.length;
  const yes = S.cards.filter(c => S.decisions[c.id] === true).length;
  const count = byId('bulkcount');
  if (count) {
    count.textContent = total
      ? `${yes} of ${total} accepted` + (yes === total ? ' — ready to save' : '')
      : '';
  }
  const yesBtn = byId('bulkyes'), noBtn = byId('bulkno');
  if (yesBtn) yesBtn.disabled = yes === total;
  if (noBtn) noBtn.disabled = yes === 0;
  drawActions();
}

function drawReview() {
  const root = byId('step-review');
  root.innerHTML = '';
  root.append($('h1', {}, txt(S.saved ? 'Saved' : 'Here is what it made of that')));
  if (S.saved) {
    const sv = S.saved;
    const done = $('div', { class: 'saved' }, [
      $('div', {}, txt(`In your profile now: ${sv.roles} roles, ${sv.years} years, `
        + `${sv.screening} screening answers.`)),
    ]);
    if (sv.missing.length) {
      // The only part intake cannot do for him, stated once, with the way to it.
      done.append($('div', { class: 'left' }, [
        txt(`${sv.missing.length} screening answers are still unset, and a run will `
          + `not start without them. They are the only thing here that has to be `
          + `you: `),
        $('a', { href: '/edit#screening' }, txt('set them in the editor →')),
      ]));
    }
    if (sv.identity.length) {
      done.append($('div', { class: 'left' }, [
        txt('An application cannot be submitted without '
          + sv.identity.map(f => f.replace('identity.', '').replace('_', ' ')).join(', ')
          + '. Saved anyway — add it in '),
        $('a', { href: '/edit#identity' }, txt('the editor')),
        txt(' or just say it here and organize again.'),
      ]));
    }
    if (sv.undated.length) {
      done.append($('div', { class: 'left' }, txt(
        sv.undated.length + ' role(s) have no start date: ' + sv.undated.join('; ')
        + '. Dates drive years-of-experience answers, so they are worth adding.')));
    }
    if ((sv.untitled || []).length) {
      done.append($('div', { class: 'left' }, txt(
        'No job title was stated for: ' + sv.untitled.join('; ')
        + '. It was left blank rather than guessed — add it and organize again.')));
    }
    if (S.moreQuestions.length) {
      done.append($('div', { class: 'left' }, [
        $('button', { class: 'tiny ghost', onclick: () => {
          S.questions = S.moreQuestions; S.moreQuestions = []; drawInterview();
        } }, txt(`it has ${S.moreQuestions.length} more questions — ask me`)),
      ]));
    }
    root.append(done);
  }
  root.append($('p', { class: 'sub' }, txt(S.saved
    ? (S.cards.length
        ? 'These are the ones you skipped. Accept any of them and save again, '
          + 'or add more material from the start.'
        : 'Everything you accepted is in the profile. Add more material any time '
          + '— it merges, it does not overwrite.')
    : 'Everything is already accepted — press save and you are done. Skip '
      + 'anything that is wrong first; each card shows which file or which '
      + 'sentence it came from.')));

  if (S.cards.length) {
    const ids = S.cards.map(c => c.id);
    // The count is live and the buttons disable when there is nothing left to
    // do. Cards arrive pre-accepted, so "accept everything" on a fresh review
    // was a genuine no-op with no feedback -- a working button that looked dead.
    root.append($('div', { class: 'bulk' }, [
      $('span', { class: 't', id: 'bulkcount' }, txt('')),
      $('button', { class: 'tiny', id: 'bulkyes',
        onclick: () => { decideMany(ids, true); msg(`accepted all ${ids.length}`, 'ok'); } },
        txt('accept everything')),
      $('button', { class: 'tiny ghost', id: 'bulkno',
        onclick: () => { decideMany(ids, false); msg(`skipped all ${ids.length}`, 'warn'); } },
        txt('skip everything')),
    ]));
  }

  if (!S.cards.length && !S.notes.length) {
    root.append($('div', { class: 'card' }, [$('p', { class: 'hint' }, txt(
      'It found nothing new to add — either the profile already has it, or the '
      + 'sources did not actually state anything it could use.'))]));
    return;
  }

  let n = 0;
  for (const [kind, label] of GROUPS) {
    const mine = S.cards.filter(c => c.kind === kind);
    if (!mine.length) continue;
    const g = $('div', { class: 'group', 'data-kind': kind });
    const yes = mine.filter(c => S.decisions[c.id] === true).length;
    const head = $('h2', {}, [txt(label),
      $('span', { class: 'n' }, txt(`· ${yes}/${mine.length}`))]);
    head.append($('span', { class: 'spacer', style: 'flex:1' }));
    const myIds = mine.map(c => c.id);
    head.append($('button', { class: 'tiny ghost', onclick: () => decideMany(myIds, true) }, txt('accept all')));
    head.append($('button', { class: 'tiny ghost', onclick: () => decideMany(myIds, false) }, txt('skip all')));
    g.append(head);

    for (const c of mine) {
      const d = S.decisions[c.id];
      const el = $('div', {
        class: 'prop' + (d === true ? ' yes' : d === false ? ' no' : ''),
        'data-id': c.id,
        style: '--i:' + (n++ % 14),
      });
      const top = $('div', { class: 'top' }, [$('span', { class: 't' }, txt(c.title))]);
      if (c.source) top.append($('span', { class: 'src' }, txt(c.source)));
      el.append(top, renderValue(c));
      if (c.replaces) el.append($('div', { class: 'was' }, txt('replaces: ' + c.replaces.slice(0, 160))));
      const acts = $('div', { class: 'acts' }, [
        $('button', { class: 'tiny' + (d === true ? ' on' : ''), 'data-act': 'accept',
          onclick: () => setDecision(c.id, true) }, txt('accept')),
        $('button', { class: 'tiny' + (d === false ? ' on' : ''), 'data-act': 'skip',
          onclick: () => setDecision(c.id, false) }, txt('skip')),
      ]);
      el.append(acts);
      g.append(el);
    }
    root.append(g);
  }

  if (S.notes.length) {
    const g = $('div', { class: 'group' });
    g.append($('h2', {}, txt('What it wants you to know')));
    S.notes.forEach((nt, i) => {
      const sev = nt.severity || 'info';
      g.append($('div', { class: 'note' + (sev === 'info' ? ' info' : ''), style: '--i:' + i }, [
        $('span', { class: 'k' }, txt(sev)), txt(nt.observation),
      ]));
    });
    root.append(g);
  }

  if (S.questions.length) {
    const g = $('div', { class: 'group' });
    g.append($('h2', {}, txt('Optional — only if you want to go deeper')));
    const box = $('div', { class: 'card' }, [
      $('p', { class: 'hint' }, txt(
        (S.saved ? 'Already saved. ' : 'Save first; this changes nothing about that. ')
        + 'It has ' + S.questions.length + ' questions that would fill in the thin '
        + 'parts, but the profile works without them.')),
    ]);
    const row = $('div', { style: 'display:flex;gap:9px;flex-wrap:wrap' }, [
      $('button', { class: 'ghost', onclick: () => drawInterview() },
        txt('Answer ' + S.questions.length + ' questions →')),
      $('button', { class: 'tiny ghost', onclick: () => { S.questions = []; drawReview(); } },
        txt("no thanks, I'm done")),
    ]);
    box.append(row);
    g.append(box);
    root.append(g);
  }
  refreshCounts();
}

/* ---------------- interview ---------------- */
function drawInterview() {
  go('interview');
  const root = byId('step-interview');
  root.innerHTML = '';
  root.append($('h1', {}, txt('A few questions')));
  root.append($('p', { class: 'sub' }, txt(
    'Answer in whatever order you like, as loosely as you like — same deal, it '
    + 'cleans up the wording and keeps your facts. Skip any that do not apply.')));
  S.questions.forEach((q, i) => {
    const box = $('div', { class: 'q' + (S.answers[i] ? ' answered' : ''), style: '--i:' + (i % 12) });
    box.append($('div', { class: 'qt' }, txt(q.question)));
    if (q.why) box.append($('div', { class: 'why' }, txt(q.why)));
    const ta = $('textarea', { placeholder: q.placeholder || 'type or dictate…' });
    ta.value = S.answers[i] || '';
    ta.addEventListener('input', () => {
      S.answers[i] = ta.value;
      box.classList.toggle('answered', !!ta.value.trim());
      drawActions();
    });
    box.append(ta);
    root.append(box);
  });
}

/* ---------------- actions bar ---------------- */
function drawActions() {
  const bar = byId('actbar');
  bar.innerHTML = '';
  const accepted = Object.values(S.decisions).filter(v => v === true).length;
  const hasSource = !!(S.dump.trim() || S.files.some(f => f.text)
    || Object.values(S.links).some(v => v.trim())
    || S.extraLinks.some(l => l.url.trim()));
  const answered = Object.values(S.answers).filter(a => a && a.trim()).length;

  if (S.step === 'sources') {
    bar.append($('button', { class: 'ghost', onclick: askQuestions }, txt('Interview me instead')));
    bar.append($('span', { class: 'spacer' }));
    bar.append($('span', { class: 'msg ' + S.msg.cls, id: 'msg' }, txt(
      S.msg.text || (hasSource ? '' : 'add a link, a resume, or a paragraph'))));
    const b = $('button', { class: 'primary', onclick: organize }, txt('Organize this →'));
    b.disabled = !hasSource;
    bar.append(b);
  } else if (S.step === 'working') {
    bar.append($('span', { class: 'msg', id: 'msg' }, txt('')));
  } else if (S.step === 'review') {
    bar.append($('button', { class: 'ghost', onclick: () => go('sources') }, txt('← back')));
    bar.append($('span', { class: 'spacer' }));
    bar.append($('span', { class: 'msg ' + S.msg.cls, id: 'msg' }, txt(S.msg.text)));
    const label = accepted ? `Save ${accepted} to profile`
      : S.saved ? (S.cards.length ? 'Accept something to save it' : 'All saved')
      : 'Nothing accepted yet';
    const b = $('button', { class: 'primary', onclick: applyAccepted }, txt(label));
    b.disabled = !accepted;
    bar.append(b);
    if (S.saved && !S.cards.length) {
      bar.append($('button', { class: 'ghost', onclick: () => {
        S.dump = ''; S.files = []; S.saved = null; S.notes = [];
        drawSources(); go('sources');
      } }, txt('Add more →')));
    }
  } else {
    bar.append($('button', { class: 'ghost', onclick: () => go(S.cards.length ? 'review' : 'sources') }, txt('← back')));
    bar.append($('span', { class: 'spacer' }));
    bar.append($('span', { class: 'msg ' + S.msg.cls, id: 'msg' }, txt(
      S.msg.text || (answered ? `${answered} answered` : 'answer at least one'))));
    const b = $('button', { class: 'primary', onclick: organizeAnswers }, txt('Organize answers →'));
    b.disabled = !answered;
    bar.append(b);
  }
}

/* ---------------- server calls ---------------- */
async function post(url, body) {
  const res = await fetch(url, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return res.json();
}

function sourcePayload() {
  const links = { ...S.links };
  S.extraLinks.forEach(l => { if (l.url.trim()) links[l.label.trim() || l.url] = l.url; });
  return {
    dump: S.dump,
    links,
    resumes: S.files.filter(f => f.text).map(f => ({ name: f.name, label: f.label, text: f.text })),
  };
}

async function organize() {
  startWorking('Organizing what you gave it');
  const out = await post('/api/intake/organize', sourcePayload());
  stopWorking();
  if (!out.ok) { go('sources'); msg(out.error || 'that did not work', 'bad'); return; }
  S.rounds++;
  S.cards = out.cards; S.notes = out.notes || []; S.questions = out.questions || [];
  S.decisions = {};
  for (const c of S.cards) S.decisions[c.id] = true;   // opt-out, not opt-in
  go('review'); drawReview();
  msg(`${S.cards.length} proposals · ${out.usage.input_tokens} in / ${out.usage.output_tokens} out tokens`);
}

async function organizeAnswers() {
  startWorking('Working your answers in');
  const payload = sourcePayload();
  payload.answers = S.questions.map((q, i) => ({ question: q.question, answer: S.answers[i] || '' }))
    .filter(a => a.answer.trim());
  const out = await post('/api/intake/organize', payload);
  stopWorking();
  if (!out.ok) { drawInterview(); msg(out.error || 'that did not work', 'bad'); return; }
  S.rounds++;
  S.cards = out.cards; S.notes = out.notes || [];
  // Do NOT put a fresh batch of questions in front of you again. One answer
  // round is enough to be useful; more only if you ask for it.
  S.moreQuestions = out.questions || [];
  S.questions = [];
  S.answers = {};
  S.decisions = {};
  for (const c of S.cards) S.decisions[c.id] = true;
  go('review'); drawReview();
}

async function askQuestions() {
  startWorking('Working out what to ask you');
  const out = await post('/api/intake/questions', { dump: S.dump });
  stopWorking();
  if (!out.ok) { go('sources'); msg(out.error || 'that did not work', 'bad'); return; }
  S.questions = out.questions || [];
  if (!S.questions.length) { go('sources'); msg('nothing to ask — the profile looks complete', 'ok'); return; }
  drawInterview();
}

async function applyAccepted() {
  const patches = S.cards.filter(c => S.decisions[c.id] === true).map(c => c.patch);
  msg('saving…');
  const out = await post('/api/intake/apply', { patches });
  if (!out.ok) {
    // The bar truncates, so a rejection has to be legible in the page itself.
    msg('not saved — see the reason above', 'bad');
    const root = byId('step-review');
    const box = $('div', { class: 'failed' }, [
      $('div', {}, txt('Not saved. Nothing was written, so nothing was lost.')),
      $('ul', {}, (out.errors || [out.error || 'unknown error']).map(e => $('li', {}, txt(e)))),
    ]);
    root.insertBefore(box, root.firstChild);
    window.scrollTo({ top: 0, behavior: 'instant' });
    return;
  }
  const m = out.missing_core || [];
  S.cards = S.cards.filter(c => S.decisions[c.id] !== true);
  Object.keys(S.decisions).forEach(k => {
    if (!S.cards.some(c => c.id === k)) delete S.decisions[k];
  });
  S.saved = {
    roles: out.roles, years: out.years, screening: out.screening_set, missing: m,
    identity: out.missing_identity || [], undated: out.undated_roles || [],
    untitled: out.untitled_roles || [],
  };
  drawReview();
  msg(m.length ? `saved · ${m.length} screening answers still need you`
               : 'saved · preflight clear', m.length ? 'warn' : 'ok');
}
"""


def render(nav_active: str = "/intake") -> bytes:
    nav = "".join(
        f'<a href="{p}"{" class=on" if p == nav_active else ""}>{n}</a>'
        for p, n in [("/", "overview"), ("/answers", "answers"),
                     ("/edit", "edit profile"), ("/intake", "intake"),
                     ("/status", "status")])
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>jobbot — intake</title>
<style>{CSS}</style></head><body>
<header><b>jobbot</b><nav>{nav}</nav>
  <span class=rail><i class=now></i><i></i><i></i></span>
</header>
<main>
  <div class=step id=step-sources></div>
  <div class=step id=step-working></div>
  <div class=step id=step-review></div>
  <div class=step id=step-interview></div>
</main>
<div class=actions><div class=inner id=actbar></div></div>
<script>{JS}
drawSources(); go('sources');
</script></body></html>""".encode()
