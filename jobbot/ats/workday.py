"""Workday: the account wall, and getting through it.

This is the highest-value adapter in the system. In the best public field data
on automated applying -- 1,503 jobs discovered, 112 applied -- 470 of 589 apply
failures (80%) were "Workday login required". Not selector rot. Not captchas.
An auth wall that no amount of DOM cleverness solves, because the answer is an
account plus an email round-trip.

Workday is kind in one respect that makes this tractable: its widgets carry
stable `data-automation-id` attributes. Those are the contract; CSS classes are
not. Every selector here is anchored to them, with text fallbacks.

Note also that Workday tenants shard across wd1/wd2/wd3/wd5/wd10/wd12/wd103 and
myworkdaysite.com. Handling only one pod is the documented reason a well-known
commercial tool "supports Workday" while failing on most real postings.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from typing import Any

import structlog

from jobbot.ats import credentials as vault
from jobbot.browser import capture as cap

log = structlog.get_logger(__name__)

# data-automation-id values, which survive Workday's own UI churn.
A = {
    "apply": "[data-automation-id='adventureButton']",
    "apply_manually": "[data-automation-id='applyManually']",
    "autofill_resume": "[data-automation-id='autofillWithResume']",
    # Tenants vary: some expose signInLink, others utilityButtonSignIn.
    "sign_in_link": "[data-automation-id='signInLink'], [data-automation-id='utilityButtonSignIn']",
    "create_account_link": "[data-automation-id='createAccountLink']",
    "email": "[data-automation-id='email']",
    "password": "[data-automation-id='password']",
    "verify_password": "[data-automation-id='verifyPassword']",
    "create_submit": "[data-automation-id='createAccountSubmitButton']",
    "signin_submit": "[data-automation-id='signInSubmitButton']",
    "consent": "[data-automation-id='createAccountCheckbox']",
    "next": "[data-automation-id='bottom-navigation-next-button']",
    "submit": "[data-automation-id='bottom-navigation-submit-button']",
    "error": "[data-automation-id='errorMessage']",
    "verify_code": "[data-automation-id='verificationCode'], input[name*='code' i]",
}


@dataclass
class AccountResult:
    created: bool
    signed_in: bool
    username: str = ""
    needed_email_code: bool = False
    error: str = ""


async def _click_first(page: Any, selectors: list[str], timeout: int = 6000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=timeout)
                await asyncio.sleep(0.6)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _text(page: Any, sel: str) -> str:
    try:
        loc = page.locator(sel).first
        if await loc.count():
            return (await loc.inner_text()).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _live(page: Any, sel: str, timeout: int = 8000) -> Any:
    """The match that is actually on screen, not the one buried under a modal.

    Clicking Workday's "Sign In" opens a modal over the page, and from that
    point there are two [data-automation-id="email"] elements. `.first` is the
    one underneath -- covered, unclickable -- which is where "element is
    covered by <DIV>" came from, and with it every Workday application in the
    run. The modal mounts last, so search from the end.
    """
    loc = page.locator(sel)
    await loc.first.wait_for(state="attached", timeout=timeout)
    for i in range(await loc.count() - 1, -1, -1):
        cand = loc.nth(i)
        with contextlib.suppress(Exception):
            if await cand.is_visible() and await cand.is_enabled():
                return cand
    return loc.first


def _plus_address(email: str, tenant: str) -> str:
    """A per-tenant alias of the same mailbox: ayaan+blueorigin@gmail.com.

    Gmail delivers it to the same inbox, so the employer can still reach the
    candidate, and it lets a fresh account be registered where the old one is
    unreachable. Already-aliased addresses are left alone.
    """
    local, _, domain = (email or "").partition("@")
    if not domain or "+" in local:
        return email
    suffix = re.sub(r"[^a-z0-9]", "", tenant.lower())[:20]
    return f"{local}+{suffix}@{domain}" if suffix else email


async def _submit(page: Any, sel: str, fallback_text: str) -> None:
    """Press a submit button that a sticky footer keeps intercepting.

    Workday's panel puts its submit under a fixed element, so the humanised
    click refuses to press it: "element is covered by <DIV>". The element is
    identified unambiguously by its automation id, so scroll to it and press
    it directly rather than abandoning the application.
    """
    btn = await _live(page, sel)
    with contextlib.suppress(Exception):
        await btn.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)
    try:
        await btn.click(timeout=6000)
        return
    except Exception:  # noqa: BLE001
        log.debug("workday.submit_intercepted", selector=sel)
    with contextlib.suppress(Exception):
        await btn.click(timeout=6000, force=True)
        return
    with contextlib.suppress(Exception):
        await _click_first(page, [f"button:has-text('{fallback_text}')"])


async def _fill_when_ready(page: Any, sel: str, value: str,
                           timeout: int = 8000) -> None:
    """Fill a field once it is actually there to be filled.

    Workday re-renders the sign-in panel as it loads, so a locator resolved a
    moment earlier detaches under us. Retrying once after the page settles
    covers the re-render.

    The retry also clears overlays first. Banners are dismissed before the
    sign-in link is clicked, but clicking it re-renders the panel and the
    banner comes back on top of the field -- which is the "element is covered"
    that took out Blue Origin, AeroVironment, GE Vernova and Allegion. The
    guard belongs here, where every one of those fills goes through.
    """
    loc = await _live(page, sel, timeout)
    try:
        await loc.fill(value)
    except Exception as first:  # noqa: BLE001
        if "covered" in str(first).lower():
            log.debug("workday.field_covered", selector=sel[:60])
        await cap.dismiss_overlays(page)
        await cap.settle(page, quiet_ms=800)
        loc = await _live(page, sel, timeout)
        await loc.fill(value)


async def _at_sign_in_gate(page: Any) -> bool:
    """Is a sign-in still standing between us and the application?"""
    for sel in (A["password"], A["signin_submit"]):
        try:
            if await page.locator(sel).first.is_visible(timeout=1500):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _sign_in(page: Any, username: str, password: str) -> tuple[bool, str]:
    """Sign in with credentials we hold. Returns (signed_in, error)."""
    await cap.dismiss_overlays(page)
    # Only reach for the sign-in link when the sign-in form is not already in
    # front of us. Workday's first step IS "Create Account/Sign In", and
    # clicking the header link there re-rendered the panel -- throwing a
    # loading overlay across the very field we were about to fill, which is
    # the "element is covered by <DIV>" that killed every Workday application.
    on_create_form = False
    with contextlib.suppress(Exception):
        on_create_form = await page.locator(A["verify_password"]).first.is_visible(
            timeout=1500)
    email_here = False
    with contextlib.suppress(Exception):
        email_here = await page.locator(A["email"]).first.is_visible(timeout=1500)

    if on_create_form or not email_here:
        await _click_first(page, [A["sign_in_link"], "a:has-text('Sign In')"], timeout=4000)
        await cap.settle(page, quiet_ms=800)
    try:
        await _fill_when_ready(page, A["email"], username)
        await _fill_when_ready(page, A["password"], password)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    await cap.settle(page, quiet_ms=600)
    await _submit(page, A["signin_submit"], "Sign In")
    await cap.settle(page, quiet_ms=1500)

    err = await _text(page, A["error"])
    if err:
        return False, err[:200]
    if await _at_sign_in_gate(page):
        # No error text, but we are demonstrably still outside. Saying
        # signed_in=True here is what walked the run into a gated form.
        return False, "sign-in did not take: still at the sign-in gate"
    return True, ""


async def start_application(page: Any) -> bool:
    """Click through from the posting to the application itself."""
    # Before anything is clicked: the cookie banner overlays the controls, and
    # a click through it fails rather than falling through to the element.
    await cap.dismiss_overlays(page)
    if not await _click_first(page, [A["apply"], "button:has-text('Apply')",
                                     "a:has-text('Apply')"]):
        return False
    await cap.settle(page, quiet_ms=900)

    # Workday offers "Autofill with Resume" vs "Apply Manually". Manual is more
    # reliable: autofill scatters values into fields we then have to audit, and
    # a wrong autofilled value is harder to detect than an empty one.
    await _click_first(page, [A["apply_manually"], "a:has-text('Apply Manually')",
                              "button:has-text('Apply Manually')"], timeout=4000)
    await cap.settle(page, quiet_ms=800)
    return True


async def needs_account(page: Any) -> bool:
    for sel in (A["create_account_link"], A["signin_submit"], A["email"],
                "text=/create account/i", "text=/sign in/i"):
        try:
            if await page.locator(sel).first.count():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def ensure_account(
    page: Any,
    *,
    tenant: str,
    email: str,
    gmail_enabled: bool = True,
    verification_timeout_s: int = 210,
) -> AccountResult:
    """Sign in if we already have an account for this tenant, else create one.

    Creating an account is done on the user's behalf and with their credentials
    stored in their own keychain. It is a real account on a real employer system
    -- so it is recorded, reusable, and never silently duplicated.
    """
    existing = vault.load("workday", tenant)

    if existing:
        log.info("workday.signing_in", tenant=tenant, username=existing.username)
        ok, err = await _sign_in(page, existing.username, existing.password)
        if ok:
            return AccountResult(created=False, signed_in=True,
                                 username=existing.username)
        # The account exists on the employer's site and the password we hold
        # does not open it -- which is what a run looks like after the keychain
        # refused to store one. Rather than stop here forever, register again
        # under a plus-address of the same inbox: same person, same mail, an
        # address the candidate controls and can be replied to.
        alias = _plus_address(email, tenant)
        if alias != existing.username:
            log.warning("workday.locked_out_registering_again", tenant=tenant,
                        was=existing.username, now=alias, error=err[:100])
            email = alias
        else:
            return AccountResult(created=False, signed_in=False,
                                 username=existing.username, error=err)

    # No account yet -- create one.
    password = vault.generate_password()
    log.info("workday.creating_account", tenant=tenant, username=email)

    await cap.dismiss_overlays(page)
    await _click_first(page, [A["create_account_link"], "a:has-text('Create Account')",
                              "button:has-text('Create Account')"], timeout=6000)
    await cap.settle(page, quiet_ms=800)

    try:
        await _fill_when_ready(page, A["email"], email)
        await _fill_when_ready(page, A["password"], password)
        # Same treatment as the two fields above: this one was still using a
        # raw fill, and failed with "Element [data-automation-id='verifyPass...']"
        # on every account creation -- the modal duplicates it too.
        if await page.locator(A["verify_password"]).count():
            await _fill_when_ready(page, A["verify_password"], password)

        # Workday's account-creation consent checkbox is required and is not
        # always labelled consistently.
        for sel in (A["consent"], "input[type=checkbox]"):
            try:
                cb = page.locator(sel).first
                if await cb.count() and await cb.is_visible():
                    await cb.check(timeout=3000)
                    break
            except Exception:  # noqa: BLE001
                continue

        # Capture the clock BEFORE submitting so a stale code cannot be accepted.
        sent_at = time.time()
        await _submit(page, A["create_submit"], "Create Account")
        await cap.settle(page, quiet_ms=1500)

        err = await _text(page, A["error"])
        if err and "already" in err.lower():
            # An account exists that we do not hold the password for.
            return AccountResult(False, False, email,
                                 error=f"account already exists for {email}: {err[:120]}")
        if err:
            return AccountResult(False, False, email, error=err[:200])

        # Save the credential the moment the tenant accepts it, BEFORE the email
        # round-trip. Saving only on the success path meant a verification
        # timeout left a real account on a real employer system whose generated
        # password existed nowhere -- locking the user out of that tenant with no
        # way to retry. A saved password for a half-verified account is
        # recoverable; a lost one is not.
        vault.save("workday", tenant, email, password)

        needed_code = False
        code_field = page.locator(A["verify_code"]).first
        if await code_field.count():
            needed_code = True
            if not gmail_enabled:
                return AccountResult(False, False, email, needed_email_code=True,
                                     error="email verification required but Gmail is disabled")

            from jobbot.mail.gmail import wait_for_code
            found = await asyncio.to_thread(
                wait_for_code,
                from_contains="workday",
                newer_than_ts=sent_at,
                timeout_s=verification_timeout_s,
            )
            if not found:
                return AccountResult(False, False, email, needed_email_code=True,
                                     error="verification code did not arrive in time")
            await code_field.fill(found.code)
            await _click_first(page, [A["signin_submit"], A["next"],
                                      "button:has-text('Submit')",
                                      "button:has-text('Verify')"])
            await cap.settle(page, quiet_ms=1200)
            log.info("workday.email_verified", tenant=tenant)

        # Creating an account does not always leave you inside it. Blue Origin
        # accepted the account, saved the credential, and returned us to the
        # sign-in page -- and because this reported signed_in=True regardless,
        # the run walked into a gated form and gave up at "still gated behind
        # sign-in". Check, and sign in with what we just created if not.
        if await _at_sign_in_gate(page):
            log.info("workday.signing_in_after_create", tenant=tenant)
            ok, err = await _sign_in(page, email, password)
            if not ok:
                return AccountResult(True, False, email,
                                     needed_email_code=needed_code, error=err)

        return AccountResult(created=True, signed_in=True, username=email,
                             needed_email_code=needed_code)

    except Exception as exc:  # noqa: BLE001
        log.warning("workday.account_failed", tenant=tenant, error=str(exc)[:200])
        return AccountResult(False, False, email, error=str(exc)[:250])


async def advance(page: Any) -> tuple[bool, str]:
    """Move to the next wizard step. Returns (moved, step_label).

    "Moved" has to mean moved. This used to end in `page.url != before or True`
    -- always True -- so a page that refused to advance reported success, and
    the caller walked the same step until it ran out of tries.
    """
    before_url = page.url
    before_step = await _text(page, "[data-automation-id='progressBarActiveStep']")
    clicked = await _click_first(page, [A["next"], "button:has-text('Save and Continue')",
                                        "button:has-text('Continue')", "button:has-text('Next')"])
    if not clicked:
        return False, before_step

    await cap.settle(page, quiet_ms=1200)
    errs = await _text(page, A["error"])
    if errs:
        return False, f"validation: {errs[:160]}"

    after_step = await _text(page, "[data-automation-id='progressBarActiveStep']")
    if page.url != before_url or (after_step and after_step != before_step):
        return True, after_step or before_step
    return False, f"still on {before_step or 'the same step'}"


async def is_final_step(page: Any) -> bool:
    try:
        return bool(await page.locator(A["submit"]).first.count())
    except Exception:  # noqa: BLE001
        return False
