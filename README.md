# jobbot

An autonomous job-application agent. It finds roles, reads each application
with a vision model, tailors a one-page resume, fills the form, verifies its own
work against the rendered page, submits, and records exactly what it said.

It is built around one rule:

> **The model may compose prose. It may never invent a fact.**

## Why that rule exists

Work authorization, visa sponsorship, criminal history, background-check
consent, veteran and disability status, education completion, arbitration
agreements — these are statements *you* make under your own name, often under an
explicit attestation of truthfulness.

In this system they are answered **only** from a confirmed value in your
profile. Never inferred, never defaulted to "yes", never guessed by the model.
If one is missing, that application halts and tells you which. If a core one is
missing, the whole run refuses to start.

This is not hypothetical caution. A widely-used open-source applier shipped
hardcoded felony and background-check answers for every user until a reviewer
caught it. A popular commercial extension is documented defaulting unknown
yes/no screeners to "yes" and submitting anyway. Both are the same bug: a system
answering a legally significant question it was never told the answer to.

## Pipeline

```
discover → dedup → ghost filter → fit filter          free, no browser
open tab → detect ATS → clear the account wall        cheap
CHECKPOINT 1  read every question off the rendered page
knockout scan would an honest answer auto-reject you?
─────────────── only now does expensive work start ───────────────
build a portfolio project → tailor resume → render 1-page PDF
fill → CHECKPOINT 2  verify + heal loop → submit
CHECKPOINT 3  did it ACTUALLY go through?
```

Stage order is the design. Expensive, irreversible work happens only after the
form is known to be reachable and winnable. The failure this avoids is
documented: one published run generated **2,019 tailored resumes to make 112
submissions**, because it tailored before discovering the form needed an account
it could not create.

## What it does

- **Discovery without scraping.** Greenhouse, Lever, Ashby, Workday,
  SmartRecruiters and Workable all serve their job boards as unauthenticated
  JSON. Plus LinkedIn/Indeed via JobSpy, with **company→ATS board resolution** —
  a LinkedIn listing with no apply link becomes a directly applicable posting on
  the company's own ATS, which converts far better than the aggregator.
- **Three vision checkpoints.** Parse, verify-and-heal, then one post-submit
  call for evidence. Checkpoint 3 exists because *silent success* is the
  dominant failure mode here — tools routinely log "applied" for jobs never
  submitted. A click is not confirmation; only on-page evidence is.
- **The account wall.** In the best public field data (1,503 jobs, 112 applied),
  **470 of 589 failures were "Workday login required"** — 80%. Not selector rot,
  not captchas. So Workday gets a real adapter: per-tenant account creation,
  passwords in the OS keychain, Gmail-based email verification.
- **One page, enforced by measurement.** The resume is rendered, its true page
  count read back, and the layout tightened until it fits — whitespace first,
  then font size, then margins, content last.
- **Fabrication checking.** Every generated resume is diffed against your
  profile. Unknown employers, unknown titles, numbers absent from your source
  bullets, and skills you don't have all block the application.
- **Ghost-job filtering.** Roughly 18–22% of postings are ghosts and ~30% of
  requisitions close with nobody hired. Filtering these saves more wasted
  applications than any resume tweak recovers.

## Quick start

```bash
uv sync
cp .env.example .env                      # add ANTHROPIC_API_KEY
cp config/profile.example.yaml config/profile.yaml   # then edit it
uv run jobbot check                       # tells you what's missing
uv run jobbot run --source greenhouse:anthropic --limit 3
```

`run` is a **dry run by default**. It fills and verifies everything and stops
before submitting. Add `--submit` only after reading `data/answers.csv`.

Full setup, including Gmail and GitHub: [docs/SETUP.md](docs/SETUP.md).

## Commands

```
jobbot check                    readiness: profile, llm, github, gmail, tracker
jobbot discover --source ...    find and rank jobs, no browser
jobbot run --source ... [--submit]
jobbot report <audit-dir>       every answer entered, field by field
jobbot ats-test --pdf x.pdf     score a resume, optionally against a live parser
jobbot github-auth              one-time consent to create repos
jobbot stats
```

Sources: `greenhouse:slug`, `lever:slug`, `ashby:slug`, `smartrecruiters:slug`,
`workable:slug`, `workday:tenant/site/pod`, `linkedin:search terms`.

## What the evidence says to optimize

Ranked by measured effect size:

1. **Referrals.** Application→interview is **40% vs 3%** inbound. Nothing in
   resume craft is within an order of magnitude. This tool deliberately does not
   automate outreach — automated sending violates LinkedIn's terms and is the
   fastest way to lose your account. It is still where your own time is best spent.
2. **Apply on the company career site**, not the aggregator: 34% of hires from
   24% of applications, versus 23% of hires from 50% via job boards.
3. **Filter for fit, then apply broadly.** Returns are roughly constant inside
   your genuine match set and collapse outside it. Interview odds plateau around
   50% of listed requirements — self-screening at 80% costs interviews.
4. **Route around silent killers**: >6-month gaps (~48% auto-screen),
   sponsorship, dropdown salary fields.

Myths this tool deliberately does **not** encode: *"75% of resumes are rejected
by ATS"* (traced to a 2012 sales pitch from a company that folded in 2013),
keyword-density scoring (no ATS vendor documents it), and the "apply within 24
hours / 5x" timing claims (one vendor blog, n≈1,610, self-selected customers,
site now a parked domain).

## The ATS score is a drafting aid, not a verdict

`jobbot ats-test` scores keyword match, skills coverage, sections,
parseability, experience clarity and impact density. Useful — but it is *this
project's* score. No ATS vendor documents scoring or rejecting a resume on
keyword density; real auto-rejection fires on structured screening **answers**,
not parsed prose. Optimize it for what it actually proxies: quantified bullets,
parseable dates, real vocabulary from the posting, clean text extraction.

## Risks, stated plainly

- **Automated submission violates the terms of service** of LinkedIn, Indeed and
  most ATS platforms. Defaults are conservative — dry run, 3 tabs, paced delays,
  a per-company cap — but the account risk is yours.
- **LinkedIn is deliberately not automated.** It is the one platform with
  documented account bans and government-ID demands for exactly this.
- **Employers are reacting.** Greenhouse ships fraud detection with identity
  verification; several large companies have reinstated in-person interviews.
  Volume is not a strategy.
- **Read what it wrote before you trust it.** `data/answers.csv` contains every
  statement made in your name.

## Using a Claude subscription instead of an API key

`JOBBOT_LLM_PROVIDER=meridian` points the client at a local proxy that bridges a
Claude subscription. It works. Note that Anthropic's Agent SDK documentation
directs third-party tools to API-key authentication rather than subscription
login, and the account risk is yours. `anthropic` is the supported path and the
default.

## License

MIT. See [NOTICE.md](NOTICE.md) for third-party credits.
