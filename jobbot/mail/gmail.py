"""Read one-time verification codes out of Gmail.

Many ATS account flows -- Workday's especially -- email a numeric code that must
be typed back into the page within a few minutes. Without this the whole
autonomous path stops at account creation, which is precisely where the
published field data says most attempts die.

Scope choice: `gmail.readonly` cannot be used alone if we also want to mark a
code as read, and `gmail.metadata` cannot read message bodies at all -- so it
cannot see a code. `gmail.modify` covers both reading bodies and marking read,
so that is the single scope requested.

Setup note that matters and bites everyone: while the Google Cloud OAuth consent
screen is in "Testing", refresh tokens expire after 7 days, so an unattended bot
silently dies every week. Publishing the app to "In production" removes that;
personal single-user apps are exempt from verification under Google's own
under-100-users allowance. You get the unverified-app warning once.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CRED_DIR = Path.home() / ".jobbot"
CLIENT_SECRET = CRED_DIR / "gmail_client_secret.json"
TOKEN_PATH = CRED_DIR / "gmail_token.json"

# Ordered by specificity: a labelled code beats a bare 6-digit run, which could
# be a ZIP code or an order number sitting elsewhere in the mail.
_CODE_PATTERNS = [
    re.compile(r"(?:verification|security|confirmation|access|one[- ]time)\s*(?:code|pin)\s*(?:is)?\s*[:\-]?\s*([0-9]{4,8})", re.I),
    re.compile(r"\bcode\s*[:\-]\s*([0-9]{4,8})\b", re.I),
    re.compile(r"\b([0-9]{6})\b\s*(?:is your|to verify|as your)", re.I),
    re.compile(r"(?:enter|use)\s+(?:this\s+)?code\s*[:\-]?\s*([0-9]{4,8})", re.I),
    re.compile(r"\b([0-9]{6})\b"),
]


class GmailError(RuntimeError):
    pass


@dataclass
class FoundCode:
    code: str
    subject: str
    sender: str
    message_id: str
    received_ts: float
    snippet: str


def _service() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    CRED_DIR.mkdir(parents=True, exist_ok=True)
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise GmailError(
                    f"missing {CLIENT_SECRET}.\n"
                    "Create an OAuth client (type: Desktop app) in Google Cloud "
                    "Console, enable the Gmail API, download the JSON, and save it "
                    "there. Publish the consent screen to 'In production' so the "
                    "refresh token does not expire every 7 days."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            # Loopback redirect; the out-of-band flow was disabled by Google.
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        TOKEN_PATH.chmod(0o600)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _body_text(payload: dict[str, Any]) -> str:
    """Walk the MIME tree and concatenate text parts."""
    out: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            try:
                txt = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
                if mime == "text/html":
                    txt = re.sub(r"<[^>]+>", " ", txt)
                out.append(txt)
            except Exception:  # noqa: BLE001
                pass
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return "\n".join(out)


def extract_code(text: str, subject: str = "") -> str | None:
    blob = f"{subject}\n{text}"
    for pat in _CODE_PATTERNS:
        m = pat.search(blob)
        if m:
            return m.group(1)
    return None


def wait_for_code(
    *,
    from_contains: str = "",
    subject_contains: str = "",
    newer_than_ts: float | None = None,
    timeout_s: int = 180,
    poll_s: int = 6,
    mark_read: bool = True,
) -> FoundCode | None:
    """Poll the inbox until a matching verification code arrives.

    `newer_than_ts` should be captured immediately BEFORE triggering the send,
    so a stale code from an earlier attempt is never accepted -- that failure is
    both silent and maddening to debug.
    """
    svc = _service()
    cutoff = newer_than_ts if newer_than_ts is not None else time.time()

    q_parts = ["newer_than:1d"]
    if from_contains:
        q_parts.append(f"from:{from_contains}")
    if subject_contains:
        q_parts.append(f'subject:"{subject_contains}"')
    query = " ".join(q_parts)

    deadline = time.time() + timeout_s
    seen: set[str] = set()

    while time.time() < deadline:
        try:
            res = svc.users().messages().list(
                userId="me", q=query, maxResults=10).execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("gmail.list_failed", error=str(exc)[:160])
            time.sleep(poll_s)
            continue

        for meta in res.get("messages", []) or []:
            mid = meta["id"]
            if mid in seen:
                continue
            seen.add(mid)

            msg = svc.users().messages().get(
                userId="me", id=mid, format="full").execute()
            internal = int(msg.get("internalDate", "0")) / 1000.0
            if internal < cutoff - 5:
                continue

            headers = {h["name"].lower(): h["value"]
                       for h in (msg.get("payload") or {}).get("headers", [])}
            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            body = _body_text(msg.get("payload") or {})
            code = extract_code(body, subject)
            if not code:
                continue

            if mark_read:
                try:
                    svc.users().messages().modify(
                        userId="me", id=mid,
                        body={"removeLabelIds": ["UNREAD"]}).execute()
                except Exception:  # noqa: BLE001
                    pass

            log.info("gmail.code_found", sender=sender[:60], subject=subject[:60])
            return FoundCode(code=code, subject=subject, sender=sender,
                             message_id=mid, received_ts=internal,
                             snippet=(msg.get("snippet") or "")[:200])

        time.sleep(poll_s)

    log.warning("gmail.code_timeout", query=query, waited_s=timeout_s)
    return None


def plus_alias(email: str, tag: str) -> str:
    """user+tag@domain, for per-company aliasing.

    Ashby in particular merges candidate records by email address, so reusing
    one address across a company's postings can silently collide with an earlier
    application. Note some ATS validators reject '+', so this is opt-in.
    """
    local, _, domain = email.partition("@")
    clean = re.sub(r"[^a-z0-9]+", "", tag.lower())[:20]
    return f"{local}+{clean}@{domain}" if clean else email
