"""Oracle Cloud HCM ("Candidate Experience") -- the ATS behind nine AmEx postings.

Every one of those was recorded "no ATS apply URL resolved; not applying via
the aggregator", because nothing here recognised the board. It is a real ATS
with a stable flow, not an aggregator:

    /job/{id}                  the posting, with an "Apply Now" button
    /job/{id}/apply/email      email + "I agree with the terms", then Next
    /job/{id}/apply/section/N  the application itself, four steps

Two things about it need saying, because both cost an afternoon:

The terms checkbox cannot be clicked. It is an `input` with zero width and
height inside a `label`, and the visible box is a styled `span` drawn over it.
A click on the label, on the span, or at the span's own coordinates all land
on the form and leave the box unticked -- the page reports "You need to agree"
and refuses to continue. Focusing the input and pressing Space ticks it.

The form ships a honeypot: a text input named `honey-pot-1`, invisible to a
person and irresistible to anything that fills every field it finds. Anything
written there marks the application as a bot. `HONEYPOT` is exported so the
field layer can leave it alone.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import structlog

from jobbot.browser import capture as cap

log = structlog.get_logger(__name__)

# Invisible by design, and filling it is the tell. Matched against a field's
# id, name and class -- Oracle uses "honey-pot-1", others spell it "honeypot".
HONEYPOT = re.compile(r"honey[\s_-]?pot", re.I)

_COOKIE = ["button:has-text('Accept All')", "button:has-text('Accept all')",
           "button:has-text('Accept Cookies')"]
_APPLY = ["button:has-text('Apply Now')", "a:has-text('Apply Now')",
          "button:has-text('Apply')"]
_NEXT = ["button:has-text('Continue')", "button:has-text('Next')",
         "button[data-bind*='next']"]
_SUBMIT = ["button:has-text('Submit')", "button:has-text('Submit Application')"]
_TERMS = "#legal-disclaimer-checkbox"


async def _click_first(page: Any, selectors: list[str], timeout: int = 5000,
                       tries: int = 4) -> bool:
    """Click the first of these that is there, waiting out the page's animation.

    The posting fades its content in, and a click during that lands on an
    element whose position is still changing -- which reads as "no Apply
    button on the posting" if the failure is swallowed. It is not missing, it
    is still moving, so retry before giving up.
    """
    last = ""
    for attempt in range(tries):
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    return True
            except Exception as exc:  # noqa: BLE001 -- try the next spelling
                last = str(exc)[:120]
                continue
        if attempt < tries - 1:
            await asyncio.sleep(1.5)
    if last:
        log.debug("oracle.click_failed", selectors=selectors[0], error=last)
    return False


async def dismiss_cookies(page: Any) -> bool:
    """The banner overlays the form, and a covered field cannot be clicked."""
    if await _click_first(page, _COOKIE):
        await cap.settle(page, quiet_ms=1200)
        return True
    return False


async def _tick_terms(page: Any) -> bool:
    """Tick "I agree with the terms and conditions".

    The control is a zero-size input behind a styled span; every click misses.
    Focus plus Space is what a keyboard user does, and it is what works.
    """
    box = page.locator(_TERMS).first
    if not await box.count():
        return True                      # no gate on this tenant
    if await box.is_checked():
        return True
    with_suppressed = (Exception,)
    try:
        await page.evaluate(
            "sel => document.querySelector(sel)?.scrollIntoView({block: 'center'})",
            _TERMS)
        await box.focus()
        await page.keyboard.press("Space")
        await cap.settle(page, quiet_ms=600)
    except with_suppressed:
        pass
    return await box.is_checked()


_CODE_FIELD = ("input[id*='verification' i]", "input[id*='code' i]",
               "input[name*='code' i]", "input[type='text'][maxlength='6']")


async def _confirm_identity(page: Any, email: str, sent_at: float,
                            gmail_enabled: bool, timeout_s: int = 180) -> tuple[bool, str]:
    """Type the code Oracle emails before it will show the application.

    The first posting opened went straight to the form; every one after it
    stopped on "Confirm Your Identity" instead. Without this the step just
    reported "still on the email step" and three attempts spent themselves
    against a page waiting for a six-digit number sitting in the inbox.
    """
    field = None
    for sel in _CODE_FIELD:
        loc = page.locator(sel).first
        with_suppressed = (Exception,)
        try:
            if await loc.count() and await loc.is_visible():
                field = loc
                break
        except with_suppressed:
            continue
    if field is None:
        return False, "identity confirmation shown but no code field found"
    if not gmail_enabled:
        return False, "email verification required but Gmail is disabled"

    from jobbot.mail.gmail import wait_for_code
    found = await asyncio.to_thread(
        wait_for_code, from_contains="", subject_contains="",
        newer_than_ts=sent_at, timeout_s=timeout_s)
    if not found:
        return False, "verification code did not arrive in time"
    await field.fill(found.code)
    await _click_first(page, ["button:has-text('Verify')"] + list(_NEXT))
    await cap.settle(page, quiet_ms=2500)
    if "/apply/email" in page.url:
        return False, f"code {found.code[:2]}.. rejected: {await _errors(page)}"
    log.info("oracle.identity_confirmed", email=email.split("@")[0] + "@...")
    return True, page.url


async def start_application(page: Any, email: str, *,
                            gmail_enabled: bool = True) -> tuple[bool, str]:
    """Get from the posting to the application form. Returns (ok, detail)."""
    await dismiss_cookies(page)

    if "/apply/" not in page.url:
        if not await _click_first(page, _APPLY):
            return False, "no Apply button on the posting"
        await cap.settle(page, quiet_ms=2500)
        await dismiss_cookies(page)

    if "/apply/email" not in page.url:
        return "/apply/" in page.url, f"at {page.url.rsplit('/', 2)[-2:]}"

    box = page.locator("input[type=email]").first
    if await box.count():
        await box.fill(email)

    if not await _tick_terms(page):
        return False, "could not tick the terms checkbox"

    sent_at = time.time()
    if not await _click_first(page, _NEXT):
        return False, "no Next button on the email step"
    await cap.settle(page, quiet_ms=3000)

    if "/apply/email" in page.url and await _wants_code(page):
        ok, detail = await _confirm_identity(page, email, sent_at, gmail_enabled)
        if not ok:
            return False, detail

    if "/apply/email" in page.url:
        # An empty reason here is useless: the step refused to advance and
        # said nothing a selector could find. Report what the page actually
        # shows so the next failure is diagnosable from the log alone.
        detail = await _errors(page) or await _visible_text(page)
        ticked = await page.locator(_TERMS).first.is_checked() \
            if await page.locator(_TERMS).first.count() else None
        return False, f"still on the email step (terms ticked={ticked}): {detail}"
    log.info("oracle.past_email_gate", url=page.url[-60:])
    return True, page.url


async def _errors(page: Any) -> str:
    with_suppressed = (Exception,)
    try:
        errs = await page.locator("[class*='error']:visible").all_inner_texts()
    except with_suppressed:
        return ""
    return " | ".join(e.strip() for e in errs if e.strip())[:160]


async def _wants_code(page: Any) -> bool:
    with_suppressed = (Exception,)
    try:
        return "confirm your identity" in (await page.inner_text("body")).lower()
    except with_suppressed:
        return False


async def _visible_text(page: Any) -> str:
    with_suppressed = (Exception,)
    try:
        body = await page.inner_text("body")
    except with_suppressed:
        return "(page text unavailable)"
    return " ".join(body.split())[:220]


def _step_of(url: str) -> str:
    m = re.search(r"/apply/section/(\d+)", url)
    return m.group(1) if m else ""


async def advance(page: Any) -> tuple[bool, str]:
    """Move to the next section. Returns (moved, step_label).

    Same contract as the Workday adapter, and the same hard-won rule: "moved"
    must mean moved, or the caller walks one step until it runs out of tries.
    """
    before = _step_of(page.url)
    if not await _click_first(page, _NEXT):
        return False, f"no Continue button on section {before or '?'}"

    await cap.settle(page, quiet_ms=1500)
    errs = await _errors(page)
    if errs:
        return False, f"validation: {errs}"

    after = _step_of(page.url)
    if after and after != before:
        return True, f"section {after}"
    return False, f"still on section {before or '?'}"


async def is_final_step(page: Any) -> bool:
    for sel in _SUBMIT:
        try:
            if await page.locator(sel).first.count():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False
