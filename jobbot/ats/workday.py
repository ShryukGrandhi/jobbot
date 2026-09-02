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


async def start_application(page: Any) -> bool:
    """Click through from the posting to the application itself."""
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
        await _click_first(page, [A["sign_in_link"], "a:has-text('Sign In')"], timeout=4000)
        await cap.settle(page, quiet_ms=600)
        try:
            await page.locator(A["email"]).first.fill(existing.username)
            await page.locator(A["password"]).first.fill(existing.password)
            await _click_first(page, [A["signin_submit"], "button:has-text('Sign In')"])
            await cap.settle(page, quiet_ms=1200)
            err = await _text(page, A["error"])
            if err:
                return AccountResult(False, False, existing.username, error=err[:200])
            return AccountResult(created=False, signed_in=True, username=existing.username)
        except Exception as exc:  # noqa: BLE001
            return AccountResult(False, False, existing.username, error=str(exc)[:200])

    # No account yet -- create one.
    password = vault.generate_password()
    log.info("workday.creating_account", tenant=tenant, username=email)

    await _click_first(page, [A["create_account_link"], "a:has-text('Create Account')",
                              "button:has-text('Create Account')"], timeout=6000)
    await cap.settle(page, quiet_ms=800)

    try:
        await page.locator(A["email"]).first.fill(email)
        await page.locator(A["password"]).first.fill(password)
        vp = page.locator(A["verify_password"]).first
        if await vp.count():
            await vp.fill(password)

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
        await _click_first(page, [A["create_submit"], "button:has-text('Create Account')"])
        await cap.settle(page, quiet_ms=1500)

        err = await _text(page, A["error"])
        if err and "already" in err.lower():
            # An account exists that we do not hold the password for.
            return AccountResult(False, False, email,
                                 error=f"account already exists for {email}: {err[:120]}")
        if err:
            return AccountResult(False, False, email, error=err[:200])

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

        vault.save("workday", tenant, email, password)
        return AccountResult(created=True, signed_in=True, username=email,
                             needed_email_code=needed_code)

    except Exception as exc:  # noqa: BLE001
        log.warning("workday.account_failed", tenant=tenant, error=str(exc)[:200])
        return AccountResult(False, False, email, error=str(exc)[:250])


async def advance(page: Any) -> tuple[bool, str]:
    """Move to the next wizard step. Returns (moved, step_label)."""
    before = page.url
    label = await _text(page, "[data-automation-id='progressBarActiveStep']")
    moved = await _click_first(page, [A["next"], "button:has-text('Save and Continue')",
                                      "button:has-text('Continue')", "button:has-text('Next')"])
    if moved:
        await cap.settle(page, quiet_ms=1200)
        errs = await _text(page, A["error"])
        if errs:
            return False, f"validation: {errs[:160]}"
        return page.url != before or True, label
    return False, label


async def is_final_step(page: Any) -> bool:
    try:
        return bool(await page.locator(A["submit"]).first.count())
    except Exception:  # noqa: BLE001
        return False
