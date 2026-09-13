# Demo video script (target: 1:50, hard cap 2:00)

Record at 1440x900 or larger. Terminal on the left half, dashboard in a browser
on the right half. No narration slides; talk over the screen.

## 0:00 – 0:15  The problem, one sentence

> "Every job-application bot I've seen has the same bug: it answers legal
> questions it was never told the answer to, and it logs 'applied' for forms
> it never submitted. jobbot is built around one rule: the model may compose
> prose, it may never invent a fact."

Show the README's pipeline block on screen while saying it.

## 0:15 – 0:40  Onboarding (app 1: the LLM)

```
uv run jobbot dashboard
```

Click **intake**. Drop a resume PDF, paste one messy paragraph, click
**Organize this**. Show the cards: each fact the model extracted, with
accept / correct per card. Say:

> "The model reads your dump and proposes a profile. You accept each fact.
> The screening block (work authorization, sponsorship, criminal history) is
> never filled by the model. It stays red until you set it yourself."

Click **edit profile**, scroll to *screening*, set one value, save.

## 0:40 – 1:00  Discovery (apps 2–7: the ATS platforms)

```
uv run jobbot discover --source greenhouse:anthropic --source ashby:openai --limit 8
```

Show the ranked table: fit score, ghost score, ATS. Say:

> "Greenhouse, Lever, Ashby, Workday, SmartRecruiters and Workable all serve
> their boards as public JSON. No scraping. Ghost postings get flagged."

Refresh **queue** in the dashboard. Tick two jobs. Blacklist one ("I'll do
these myself").

## 1:00 – 1:35  Apply, dry run (apps: ATS form, Gmail, GitHub, keychain)

```
uv run jobbot run --source greenhouse:anthropic --approved --limit 1
```

Let the browser open. Talk over the three checkpoints as they happen:

> "Checkpoint 1 reads every question off the rendered page, with vision.
> Knockout scan: would an honest answer auto-reject you? Only now does it
> tailor the resume, render it, measure that it is one page, and fill.
> Checkpoint 2 verifies the filled page against what it meant to type and
> heals. It stops before submit, because this is a dry run."

If the run hits a Workday posting instead, show the account wall: it
creates the tenant account, stores the password in the OS keychain, and
reads the verification code out of Gmail.

## 1:35 – 1:50  The ledger + reliability

Click **answers** in the dashboard. Say:

> "Every statement made in your name, with its source and confidence. Blanks
> say why they are blank."

Cut to the terminal:

```
uv run pytest -q
```

> "82 unit tests, CI on Linux, macOS and Windows, a dashboard self-check
> that renders every view and attacks the path guard, and a live end-to-end
> spec that drives the real server, real boards, and a real browser."

## 1:50 – 2:00  Close

> "Three vision checkpoints, seven external systems, zero invented facts."

Show the GitHub repo URL.

---

## Recording checklist

- `.env` has a working `ANTHROPIC_API_KEY` (or `JOBBOT_LLM_PROVIDER=gemini` + key)
- `config/profile.yaml` is a real profile with the screening block set
- `data/` is empty before recording (`rm -rf data/*; touch data/.gitkeep`)
- Dashboard open at `/intake` before you hit record
- Have one resume PDF ready on the desktop
- Do NOT pass `--submit` on camera
