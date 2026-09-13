# Setup

## 1. Install

```bash
git clone https://github.com/<you>/jobbot.git
cd jobbot
uv sync                      # or: python -m venv .venv && pip install -e .
```

Python 3.12+. On first browser launch CloakBrowser downloads a ~200MB patched
Chromium into `~/.cloakbrowser`.

## 2. Configure

```bash
cp .env.example .env
cp config/profile.example.yaml config/profile.yaml
```

Edit both. `.env` needs at minimum an `ANTHROPIC_API_KEY`.

`config/profile.yaml` is the important one — read the comments in it. Every
statement made in your name comes from that file.

## 3. Check

```bash
uv run jobbot check
```

This tells you exactly what is missing: unset legally-significant answers, a
missing API key, an unauthenticated GitHub, an unconfigured Gmail. Fix what it
lists before running.

## 4. Dry run

```bash
uv run jobbot discover --source greenhouse:anthropic --source ashby:openai
uv run jobbot run --source greenhouse:anthropic --limit 3
```

`run` is a **dry run by default** — it fills and verifies the whole form, then
stops before submitting. Read `data/answers.csv` and the per-application audit
directory before you trust it with `--submit`.

## 5. Optional integrations

**Gmail (for verification codes).** Workday and similar mail you a code during
account creation. Create a Desktop-app OAuth client in Google Cloud Console,
enable the Gmail API, and save the JSON to
`~/.jobbot/gmail_client_secret.json`. Publish the consent screen to "In
production" or your refresh token expires every 7 days.

**GitHub (for per-application portfolio projects).** `gh auth login`, then
`uv run jobbot github-auth` once to record consent. This creates real public
repositories under your account.

**Notifications.** Set `JOBBOT_NOTIFY_TO` and one of `JOBBOT_NOTIFY_CMD` or
`JOBBOT_NOTIFY_URL`. On macOS with Messages signed in, it works with no command
configured. With nothing set, notifications go to `data/notifications.log`.

## Where your data lives

| Path | What |
|---|---|
| `data/applications.csv` | One row per job: status, scores, paths |
| `data/answers.csv` | One row per answer — everything said in your name |
| `data/applications/<job>/` | Screenshots, resume PDF, form JSON, verification |
| `data/notifications.log` | Every message sent or attempted |
| `data/lessons.jsonl` | What each run learned about each ATS |

All gitignored. None of it leaves your machine except the application itself.
