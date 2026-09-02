# Architecture

## The seam that matters

The answering layer speaks in `FormField` and `ProposedAnswer`. It never sees a
DOM element. Selector rot is therefore confined to one adapter per ATS, and the
model cannot misidentify a control.

Labels resolve through `label[for=id]`, then `aria-labelledby`, then
`aria-label`, then a wrapping `<label>`, and only then proximity. That ordering
prevents the classic failure where a phone number lands in the Last Name box —
a silent data corruption that is worse than a crash, because it submits.

## Module map

| Module | Job |
|---|---|
| `profile.py` | The candidate record. Provenance-typed screening answers. |
| `discovery/sources.py` | ATS public APIs — Greenhouse, Lever, Ashby, Workday, ... |
| `discovery/aggregator.py` | JobSpy + ATS link recovery + company→board resolution |
| `ats/detect.py` | Which ATS a URL belongs to, incl. embedded boards |
| `ats/workday.py` | The account wall: per-tenant signup, email verification |
| `ats/credentials.py` | Per-tenant passwords in the OS keychain |
| `browser/session.py` | One persistent stealth context, hard tab budget |
| `browser/capture.py` | Scroll-primed viewport tiling + aria snapshot |
| `forms/extract.py` | DOM → `FormField` with selectors |
| `forms/matching.py` | Snap a value onto a real option; fail closed |
| `forms/fill.py` | Per-control write strategies with verification |
| `healer/answer.py` | Deterministic mapping, then the model for the rest |
| `healer/checkpoints.py` | The three vision checkpoints + heal loop |
| `resume/tailor.py` | Tailor, critique, revise. Fabrication check. |
| `resume/ats_score.py` | Measured match score driving refinement |
| `resume/render.py` | HTML → one-page PDF via CDP, enforced by measurement |
| `ghproj/` | Per-application portfolio projects, real commits |
| `tracker/` | `applications.csv` index + `answers.csv` detail |
| `notify.py` | Message per submission, pluggable backends |

## Why capture tiles instead of `full_page=True`

Chromium rasters into a surface capped by max texture size (commonly 16,384px).
Taller pages truncate or silently repeat a band. Playwright also does not scroll
before capturing, so lazy content is absent, and `position: fixed` headers land
wrong. So: scroll to force render, then capture viewport tiles at known offsets,
and pair every capture with an accessibility snapshot. Pixels are better for
layout; the a11y tree is better for what is answerable.

Capture must cover the whole page. The verifier fails closed on anything it
cannot see, so a truncated capture looks exactly like a failed fill.

## Why three checkpoints

Silent success is the dominant failure mode in this category. Tools routinely
log "applied" for jobs that were never submitted. Checkpoint 3 therefore
requires affirmative on-page evidence — a confirmation message or reference
number — before anything is recorded as confirmed. A click is not confirmation.

## Cost ordering

Discovery, ghost filtering, fit filtering and the knockout scan all run before
any resume is tailored or any project is built. One published run generated
2,019 tailored resumes to make 112 submissions by doing this backwards.
