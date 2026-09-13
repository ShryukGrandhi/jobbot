---
name: job-apply
description: Find, tailor, and apply to jobs with jobbot. Use when the user wants to search for roles, tailor a resume to a job description, check a resume against ATS parsers, fill out an application, or run an autonomous application session. Also use for setting up jobbot for the first time.
---

# Applying to jobs with jobbot

You are driving `jobbot`, a local agent that applies to jobs on the user's
behalf. Everything it says goes out under the user's real name, so the rules
below are not style preferences.

## The one rule

**Compose prose. Never invent a fact.**

Work authorization, visa sponsorship, citizenship, criminal history,
background-check and drug-test consent, veteran and disability status, age,
education completion, licensure, arbitration agreements — answer these **only**
from a confirmed value in `config/profile.yaml`. Never infer one, never default
to "yes", never let a model choose. If one is missing, stop and ask the user.

If you catch yourself about to write a plausible value for a field you don't
have data for, that is the bug this whole system exists to prevent.

## First run

```bash
uv sync
cp .env.example .env                                  # ANTHROPIC_API_KEY
cp config/profile.example.yaml config/profile.yaml
uv run jobbot check
```

`check` prints exactly what is missing. Fix everything it lists before running.
If the user has a resume PDF, read it and fill `config/profile.yaml` from it —
copy their real bullets and numbers verbatim, and leave the entire `screening`
block for them to confirm. Ask for those answers; do not fill them in yourself.

## Normal use

```bash
uv run jobbot discover --source greenhouse:<slug> --source ashby:<slug>
uv run jobbot run --source greenhouse:<slug> --limit 5     # DRY RUN
uv run jobbot run --source greenhouse:<slug> --limit 5 --submit
uv run jobbot report data/applications/<job_id>/           # every answer
uv run jobbot ats-test --pdf <resume.pdf> --lever-url <apply-url>
```

`run` is a dry run unless `--submit` is passed. Never add `--submit` on the
user's behalf without them asking for it in that turn. Submissions cannot be
retracted.

## Before you let it submit

1. `uv run jobbot report <audit-dir>` and read the answers out loud to the user.
2. Check `data/answers.csv` for anything marked `left_blank`.
3. Confirm the resume is one page and the fabrication check passed.
4. Only then offer `--submit`.

## Writing anything in the user's voice

Cover-letter answers, "why us" essays, and resume bullets must pass the
portability test: if the sentence could move unchanged to another candidate or
company, it is filler. Replace it with a number, a mechanism, or a specific
decision from their real work.

No "leveraged", "passionate about", "a testament to", "it's not X it's Y", no
throat-clearing openers, no fake-profound closers. See `jobbot/style.py`.

## When something breaks

- **Verification won't come clean** — read `verification.json` in the audit dir.
  The verifier fails closed on anything it cannot see, so check whether capture
  covered the whole page before assuming the fill failed.
- **A field is left blank** — check `blank_reason` in `data/answers.csv`. If it
  says a legally significant value is unset, the fix is in `profile.yaml`, not
  in code.
- **Workday needs an account** — that's expected; it creates one per tenant and
  stores the password in the OS keychain. It needs Gmail configured for the
  verification code.

## What not to do

- Don't automate LinkedIn Easy Apply or LinkedIn outreach. Documented account
  bans, and it's a terms violation.
- Don't chase volume. Employers now run fraud detection at intake.
- Don't edit `config/profile.yaml` screening answers on the user's behalf.
