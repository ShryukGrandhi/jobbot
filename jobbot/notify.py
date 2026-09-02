"""Send a text when an application is submitted.

Backends are tried in order and the first one that works wins:

  command    -- a user-supplied command or HTTP endpoint (configured, not guessed)
  imessage   -- macOS Messages.app via AppleScript; works on a signed-in Mac
  webhook    -- generic POST, for Twilio-style relays
  log        -- always succeeds, writes to data/notifications.log

The log backend is deliberately last and unconditional. A notification failure
must never abort or mask an application result -- the point of the message is to
tell you what already happened.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_TO = os.environ.get("JOBBOT_NOTIFY_TO", "")
LOG_PATH = Path("data/notifications.log")


@dataclass
class Notification:
    company: str
    title: str
    url: str
    match_score: float          # fit against the profile, 0-1
    ats_score: float            # tailored resume vs this JD, 0-100
    reliability: float          # confidence the submission actually landed, 0-1
    status: str
    evidence: str = ""
    answered: int = 0
    total_fields: int = 0
    flagged: int = 0
    heal_rounds: int = 0
    project_url: str = ""

    def text(self) -> str:
        """Short enough for one SMS segment where possible, still specific."""
        bar = "APPLIED" if self.status in ("confirmed", "submitted") else self.status.upper()
        lines = [
            f"[{bar}] {self.title[:52]} @ {self.company[:28]}",
            f"match {self.match_score:.0%} | ATS {self.ats_score:.0f}/100 | "
            f"reliability {self.reliability:.0%}",
            f"fields {self.answered}/{self.total_fields}"
            + (f", {self.flagged} left blank" if self.flagged else "")
            + (f", {self.heal_rounds} fix round(s)" if self.heal_rounds else ""),
        ]
        if self.evidence:
            lines.append(f'confirmation: "{self.evidence[:70]}"')
        elif self.status == "submitted":
            lines.append("NO on-page confirmation seen - verify manually")
        if self.project_url:
            lines.append(f"project: {self.project_url}")
        lines.append(self.url[:90])
        return "\n".join(lines)


def reliability_score(*, submitted: bool, evidence: str, heal_rounds: int,
                      flagged: int, total_fields: int, verified: bool) -> float:
    """How much to trust that this application actually landed, and correctly.

    Deliberately harsh about the difference between clicking submit and
    observing a confirmation. Silent success is the dominant failure mode in
    this category of tool, so an unconfirmed submit is capped well below one
    with on-page evidence.
    """
    if not submitted:
        return 0.0
    score = 0.45
    if evidence.strip():
        score += 0.35                      # affirmative on-page proof
    if verified:
        score += 0.10                      # passed pre-submit verification cleanly
    score -= min(0.15, 0.05 * heal_rounds)  # needed fixing = less certain
    if total_fields:
        score -= min(0.20, 0.6 * (flagged / total_fields))   # blanks left behind
    return round(max(0.0, min(1.0, score)), 2)


# -- backends -------------------------------------------------------------

def _via_command(to: str, body: str) -> bool:
    """User-configured command or HTTP endpoint. Never guessed.

    JOBBOT_CLAWDBOT_CMD  -- shell command; {to} and {body} are substituted
    JOBBOT_CLAWDBOT_URL  -- POST {"to":..., "body":...}
    """
    cmd = os.environ.get("JOBBOT_NOTIFY_CMD") or os.environ.get("JOBBOT_CLAWDBOT_CMD")
    if cmd:
        try:
            filled = cmd.replace("{to}", shlex.quote(to)).replace("{body}", shlex.quote(body))
            r = subprocess.run(filled, shell=True, capture_output=True,
                               text=True, timeout=45)
            if r.returncode == 0:
                return True
            log.warning("notify.clawdbot_cmd_failed", rc=r.returncode,
                        err=(r.stderr or "")[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("notify.clawdbot_cmd_error", error=str(exc)[:160])

    url = os.environ.get("JOBBOT_NOTIFY_URL") or os.environ.get("JOBBOT_CLAWDBOT_URL")
    if url:
        try:
            import httpx
            headers = {}
            tok = os.environ.get("JOBBOT_NOTIFY_TOKEN") or os.environ.get("JOBBOT_CLAWDBOT_TOKEN")
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            r = httpx.post(url, json={"to": to, "body": body},
                           headers=headers, timeout=30)
            if r.status_code < 300:
                return True
            log.warning("notify.clawdbot_http_failed", status=r.status_code,
                        body=r.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("notify.clawdbot_http_error", error=str(exc)[:160])
    return False


def _via_imessage(to: str, body: str) -> bool:
    """Messages.app on macOS.

    Two syntaxes, because the AppleScript Messages dictionary is inconsistent
    across versions. `buddy ... of <service>` is the one that works reliably on
    current macOS; `participant` is kept as a fallback. Requires Messages signed
    in and Automation permission for the calling process.
    """
    if os.uname().sysname != "Darwin":
        return False

    msg = json.dumps(body)          # safe AppleScript string literal
    recipient = json.dumps(to)

    scripts = [
        f'''tell application "Messages"
             set svc to 1st service whose service type = iMessage
             send {msg} to buddy {recipient} of svc
           end tell''',
        f'''tell application "Messages"
             send {msg} to participant {recipient}
           end tell''',
    ]
    for script in scripts:
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=45)
            if r.returncode == 0 and not (r.stderr or "").strip():
                return True
            log.debug("notify.imessage_variant_failed", err=(r.stderr or "")[:160])
        except Exception as exc:  # noqa: BLE001
            log.debug("notify.imessage_error", error=str(exc)[:160])
    return False


def _via_webhook(to: str, body: str) -> bool:
    url = os.environ.get("JOBBOT_WEBHOOK_URL")
    if not url:
        return False
    try:
        import httpx
        r = httpx.post(url, json={"to": to, "text": body}, timeout=30)
        return r.status_code < 300
    except Exception as exc:  # noqa: BLE001
        log.warning("notify.webhook_error", error=str(exc)[:160])
        return False


def _via_log(to: str, body: str) -> bool:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"--- {datetime.now(timezone.utc).isoformat(timespec='seconds')} to {to}\n")
        fh.write(body + "\n\n")
    return True


BACKENDS = [
    ("command", _via_command),
    ("imessage", _via_imessage),
    ("webhook", _via_webhook),
]


def send(n: Notification, *, to: str | None = None, dry_run: bool = False) -> str:
    """Deliver a notification. Returns the backend that handled it."""
    to = to or DEFAULT_TO
    body = n.text()
    if not to:
        _via_log("(unset)", body)
        log.info("notify.no_recipient", hint="set JOBBOT_NOTIFY_TO to receive messages")
        return "log(no-recipient)"

    if dry_run:
        _via_log(to, "[DRY RUN] " + body)
        log.info("notify.dry_run", to=to)
        return "log(dry-run)"

    for name, fn in BACKENDS:
        try:
            if fn(to, body):
                _via_log(to, f"[sent via {name}] " + body)
                log.info("notify.sent", backend=name, to=to, company=n.company)
                return name
        except Exception as exc:  # noqa: BLE001
            log.warning("notify.backend_error", backend=name, error=str(exc)[:160])

    _via_log(to, "[NOT DELIVERED] " + body)
    log.warning("notify.undelivered", to=to, company=n.company)
    return "log(undelivered)"


@dataclass
class RunSummary:
    """End-of-run digest."""
    applied: int = 0
    confirmed: int = 0
    needs_human: int = 0
    knockout: int = 0
    unreachable: int = 0
    failed: int = 0
    avg_match: float = 0.0
    avg_ats: float = 0.0
    avg_reliability: float = 0.0
    duration_min: float = 0.0
    companies: list[str] | None = None

    def text(self) -> str:
        lines = [
            f"jobbot run finished ({self.duration_min:.0f} min)",
            f"submitted {self.applied} ({self.confirmed} with on-page confirmation)",
        ]
        skipped = []
        if self.knockout:
            skipped.append(f"{self.knockout} knockout")
        if self.needs_human:
            skipped.append(f"{self.needs_human} needs-you")
        if self.unreachable:
            skipped.append(f"{self.unreachable} unreachable")
        if self.failed:
            skipped.append(f"{self.failed} failed")
        if skipped:
            lines.append("skipped: " + ", ".join(skipped))
        if self.applied:
            lines.append(f"avg match {self.avg_match:.0%} | avg ATS {self.avg_ats:.0f}/100 "
                         f"| avg reliability {self.avg_reliability:.0%}")
        if self.companies:
            lines.append("applied to: " + ", ".join(self.companies[:8])
                         + ("..." if len(self.companies) > 8 else ""))
        return "\n".join(lines)


def send_summary(s: RunSummary, *, to: str | None = None) -> str:
    to = to or DEFAULT_TO
    body = s.text()
    if not to:
        _via_log("(unset)", body)
        return "log(no-recipient)"
    for name, fn in BACKENDS:
        try:
            if fn(to, body):
                _via_log(to, f"[summary via {name}] " + body)
                log.info("notify.summary_sent", backend=name, applied=s.applied)
                return name
        except Exception as exc:  # noqa: BLE001
            log.warning("notify.summary_backend_error", backend=name, error=str(exc)[:160])
    _via_log(to, "[SUMMARY NOT DELIVERED] " + body)
    return "log(undelivered)"
