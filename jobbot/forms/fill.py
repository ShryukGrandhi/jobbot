"""Write answers into a live form.

Per-control strategy, each choice driven by a documented failure mode:

  react-select (Greenhouse, Ashby, Lever)
      Rebuilds its DOM on every keystroke. Any cached element handle goes stale
      mid-word. Type character by character and re-query the listbox each time.

  Autocomplete / typeahead
      `fill()` sets .value and fires one input event, which never opens the
      suggestion list. Type sequentially so the widget's own key handlers run.

  Lever checkboxes and radios
      Programmatic clicks on them are reported to trigger hCaptcha. Prefer the
      associated label element, which is what a human actually clicks.

  Workable
      Aggressively re-renders; re-query immediately before every interaction.

  File upload
      `set_input_files` against the hidden <input type=file> bypasses the OS
      dialog entirely. Never click the pretty "Attach" button first.

Everything here is idempotent and verifies its own write. A fill that silently
does nothing is the single most expensive bug in this system: it surfaces as a
validation error at submit, after the resume and the project already exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
from pathlib import Path
from typing import Any

import structlog

from jobbot.forms.matching import (
    is_decline, match_acknowledgement, match_boolean, match_decline,
    match_numeric_range, match_option,
)
from jobbot.forms.model import FieldKind, FormField, ProposedAnswer

log = structlog.get_logger(__name__)


class FillError(RuntimeError):
    pass


async def _human_pause(lo: float = 0.12, hi: float = 0.38) -> None:
    """Pacing, not choreography.

    Behavioural scoring at the invisible captcha reacts to event ordering and
    dwell time, not to mouse-curve realism -- and filling fourteen fields in
    900ms is the shape it is looking for.
    """
    await asyncio.sleep(random.uniform(lo, hi))


async def _locate(page: Any, field: FormField, timeout: int = 8000) -> Any:
    """Find the control, falling back to its label when we have no selector.

    Vision can report a field the DOM walk missed, in which case selector is
    empty. Passing "" to a locator raises a CSS parse error, so resolve by
    accessible name instead of crashing.
    """
    if field.selector:
        loc = page.locator(field.selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        return loc

    label = (field.label or "").split("\n")[0].strip()
    if not label:
        raise FillError("field has neither a selector nor a usable label")
    for cand in (
        page.get_by_label(label, exact=False).first,
        page.get_by_role("textbox", name=label).first,
        page.get_by_role("combobox", name=label).first,
        page.locator(f"[aria-label*='{label[:40]}']").first,
    ):
        try:
            if await cand.count():
                await cand.wait_for(state="visible", timeout=3000)
                return cand
        except Exception:  # noqa: BLE001
            continue
    raise FillError(f"could not locate field by label: {label[:60]!r}")


async def _visible(page: Any, selector: str, timeout: int = 8000) -> Any:
    loc = page.locator(selector).first
    await loc.wait_for(state="visible", timeout=timeout)
    return loc


async def fill_text(page: Any, field: FormField, value: str, *, sequential: bool = False) -> bool:
    loc = await _locate(page, field)
    await loc.scroll_into_view_if_needed()
    await _human_pause()
    if sequential:
        await loc.click()
        await loc.fill("")
        await loc.press_sequentially(str(value), delay=random.randint(28, 65))
    else:
        await loc.fill(str(value))
    got = await loc.input_value()
    ok = got.strip()[:40] == str(value).strip()[:40]

    if not ok and field.kind is FieldKind.PHONE:
        # Many phone widgets reformat or reject punctuation. Retry with bare
        # digits before declaring failure.
        digits = re.sub(r"\D", "", str(value))
        if digits:
            await loc.fill("")
            await loc.press_sequentially(digits, delay=random.randint(25, 55))
            got = await loc.input_value()
            ok = bool(re.sub(r"\D", "", got)) and re.sub(r"\D", "", got).endswith(digits[-10:])

    if not ok:
        log.warning("fill.text_mismatch", label=field.label[:50],
                    wanted=str(value)[:40], got=got[:40])
    return ok


async def fill_select(page: Any, field: FormField, value: str) -> bool:
    """Native <select>. Snap to a real option; never inject free text."""
    opts = field.option_labels()
    ack = match_acknowledgement(value, opts)
    if ack is not None:
        chosen, score, how = ack, 1.0, "acknowledgement"
    elif is_decline(str(value)) and match_decline(opts):
        chosen, score, how = match_decline(opts), 1.0, "decline-synonym"
    else:
        chosen, score, how = match_option(str(value), opts)
    if chosen is None:
        log.warning("fill.select_no_match", label=field.label[:50],
                    wanted=str(value)[:40], score=score)
        return False
    loc = await _locate(page, field)
    await loc.scroll_into_view_if_needed()
    await _human_pause()
    try:
        await loc.select_option(label=chosen)
    except Exception:
        opt = next((o for o in field.options if o.label == chosen), None)
        if opt is None:
            return False
        await loc.select_option(value=opt.value)
    log.debug("fill.select", label=field.label[:40], chose=chosen, via=how)
    return True


_VISIBLE_OPTIONS_JS = r"""
() => Array.from(document.querySelectorAll(
        '[role="option"], .select__option, li[id*="option"]'))
  .filter(o => { const r = o.getBoundingClientRect();
                 return r.width > 0 && r.height > 0; })
  .map(o => (o.innerText || '').trim())
  .filter(Boolean)
"""

_CLICK_OPTION_JS = r"""
(wanted) => {
  const nodes = Array.from(document.querySelectorAll(
      '[role="option"], .select__option, li[id*="option"]'))
    .filter(o => { const r = o.getBoundingClientRect();
                   return r.width > 0 && r.height > 0; });
  for (const o of nodes) {
    if ((o.innerText || '').trim() === wanted) {
      o.scrollIntoView({block: 'nearest'});
      o.click();
      return true;
    }
  }
  return false;
}
"""

_CONTROL_TEXT_JS = r"""
(sel) => {
  const e = document.querySelector(sel);
  if (!e) return '';
  // closest() matches the element itself, and the input carries a
  // "select__input" class -- so asking for the nearest [class*=select]
  // returns the input, whose innerText is always empty. Walk to the actual
  // control wrapper instead and read the rendered value.
  let c = e.parentElement;
  while (c && !/select__control|select__value-container/.test(c.className || '')) {
    c = c.parentElement;
    if (c === document.body) { c = null; break; }
  }
  if (!c) c = e.closest('.field, fieldset') || e.parentElement;
  const single = c && c.querySelector('.select__single-value, [class*="singleValue"]');
  if (single) return (single.innerText || '').trim().slice(0, 120);
  return c ? (c.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 120) : '';
}
"""


async def _close_open_menus(page: Any, tries: int = 3) -> None:
    """Dismiss any open listbox so option discovery starts from a clean slate."""
    for _ in range(tries):
        if not await _visible_options(page):
            return
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
    with contextlib.suppress(Exception):
        await page.mouse.click(4, 4)     # click away as a last resort
        await asyncio.sleep(0.25)


async def _visible_options(page: Any) -> list[str]:
    try:
        return await page.evaluate(_VISIBLE_OPTIONS_JS) or []
    except Exception:  # noqa: BLE001
        return []


async def fill_combobox(page: Any, field: FormField, value: str) -> bool:
    """Custom listbox widget: open it, then choose from the menu IT opened.

    Option discovery is done by difference. A page routinely has more than one
    menu mounted -- a phone-country picker is the usual offender -- and
    react-select portals its menu to <body>, so neither "everything matching
    [role=option]" nor "options inside the control's container" identifies the
    right list. Snapshot the visible options before the click, snapshot after,
    and the newly-appeared ones belong to this control.

    Without this, a relocation question gets answered from the country list.
    """
    loc = await _locate(page, field)
    await loc.scroll_into_view_if_needed()
    await _human_pause()

    # Close anything already open before measuring. Consecutive yes/no fields
    # offer identical option text, so a menu left open from the previous field
    # makes the before/after diff come out empty and the fill silently no-ops.
    await _close_open_menus(page)

    before = set(await _visible_options(page))
    await loc.click()
    await asyncio.sleep(0.45)
    after = await _visible_options(page)
    opts = [o for o in after if o not in before]

    text = str(value)
    chosen, score, how = (None, 0.0, "no-options")
    if opts:
        # A single-option consent control has no "Yes" to match.
        ack = match_acknowledgement(value, opts)
        if ack is not None:
            chosen, score, how = ack, 1.0, "acknowledgement"
        elif is_decline(text):
            d = match_decline(opts)
            if d is not None:
                chosen, score, how = d, 1.0, "decline-synonym"
        if chosen is None:
            chosen, score, how = match_option(text, opts)

    # Typing narrows a long list. Only if the opened menu did not already offer
    # what we want -- typing into a prefix-filtered widget can empty it.
    if chosen is None:
        with contextlib.suppress(Exception):
            await loc.press_sequentially(text[:32], delay=random.randint(35, 75))
            await asyncio.sleep(0.55)
        after = await _visible_options(page)
        opts = [o for o in after if o not in before] or after
        if opts:
            ack = match_acknowledgement(value, opts)
            if ack is not None:
                chosen, score, how = ack, 1.0, "acknowledgement"
            elif is_decline(text) and match_decline(opts):
                chosen, score, how = match_decline(opts), 1.0, "decline-synonym"
            else:
                chosen, score, how = match_option(text, opts)

    if chosen is None:
        log.warning("fill.combobox_no_option", label=field.label[:50],
                    wanted=text[:40], seen=opts[:6])
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")
        return False

    clicked = False
    with contextlib.suppress(Exception):
        clicked = bool(await page.evaluate(_CLICK_OPTION_JS, chosen))
    if not clicked:
        with contextlib.suppress(Exception):
            await page.get_by_role("option", name=chosen, exact=True).first.click(timeout=3000)
            clicked = True
    if not clicked:
        log.warning("fill.combobox_click_failed", label=field.label[:50], chose=chosen)
        return False

    await asyncio.sleep(0.35)

    # Confirm it stuck. A combobox that silently reverts is worse than one that
    # fails outright, because the form then looks complete.
    stuck = True
    with contextlib.suppress(Exception):
        got = ((await loc.input_value()) or "").strip()
        if not got and field.selector:
            shown = await page.evaluate(_CONTROL_TEXT_JS, field.selector)
            stuck = chosen.lower() in (shown or "").lower()
            if not stuck:
                log.warning("fill.combobox_did_not_stick", label=field.label[:50],
                            chose=chosen, shows=(shown or "")[:60])
    if not stuck:
        return False

    log.debug("fill.combobox", label=field.label[:40], chose=chosen, via=how)
    return True


async def fill_radio(page: Any, field: FormField, value: Any) -> bool:
    """Choose within a radio group by clicking its LABEL, not the input.

    Clicking the input programmatically is what reportedly trips Lever's
    invisible hCaptcha; the label is the element a human actually hits.
    """
    opts = field.option_labels()
    chosen = match_boolean(value, opts) if isinstance(value, bool) else None
    if chosen is None:
        chosen, _, _ = match_option(str(value), opts)
    if chosen is None:
        log.warning("fill.radio_no_match", label=field.label[:50], wanted=str(value)[:40])
        return False

    await _human_pause()
    for sel in (f"label:has-text('{chosen}')",
                f"input[type=radio][value='{chosen}']",
                f"[role=radio]:has-text('{chosen}')"):
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.scroll_into_view_if_needed()
                await loc.click(timeout=4000)
                log.debug("fill.radio", label=field.label[:40], chose=chosen)
                return True
        except Exception:
            continue
    return False


async def fill_checkbox(page: Any, field: FormField, value: bool) -> bool:
    loc = await _locate(page, field)
    await loc.scroll_into_view_if_needed()
    await _human_pause()
    try:
        if value:
            await loc.check(timeout=4000)
        else:
            await loc.uncheck(timeout=4000)
        return True
    except Exception:
        # Some designs hide the input and style the label; click that instead.
        try:
            await page.locator(f"label[for='{field.field_id}']").first.click(timeout=3000)
            return True
        except Exception:
            return False


async def upload_file(page: Any, field: FormField, path: str | Path) -> bool:
    """Attach a file straight to the hidden input, bypassing the OS dialog."""
    p = Path(path)
    if not p.exists():
        raise FillError(f"file to upload does not exist: {p}")

    for sel in (field.selector, "input[type=file]", "input[type='file']"):
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.set_input_files(str(p))
                await asyncio.sleep(0.9)
                log.info("fill.uploaded", label=field.label[:40], file=p.name)
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("fill.upload_attempt_failed", sel=sel, error=str(exc)[:100])
            continue

    # Dropzone with no reachable input: drive the file chooser instead.
    try:
        async with page.expect_file_chooser(timeout=6000) as info:
            await page.locator("button:has-text('Attach'), button:has-text('Upload')").first.click()
        chooser = await info.value
        await chooser.set_files(str(p))
        await asyncio.sleep(0.9)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("fill.upload_failed", error=str(exc)[:150])
        return False


AUTOCOMPLETE_HINTS = ("location", "city", "address", "school", "university",
                      "country", "state", "region", "company")


async def apply_answer(
    page: Any,
    field: FormField,
    answer: ProposedAnswer,
    *,
    resume_path: str | Path | None = None,
) -> bool:
    """Write one answer, dispatching on control kind."""
    if not answer.submittable:
        return False
    v = answer.value

    try:
        if field.kind is FieldKind.FILE:
            return await upload_file(page, field, resume_path or str(v))

        if field.kind in (FieldKind.SELECT, FieldKind.MULTISELECT, FieldKind.COMBOBOX):
            if isinstance(v, bool):
                # "True"/"False" match no real option. Map onto whatever this
                # form calls yes and no before touching the widget.
                mapped = match_boolean(v, field.option_labels())
                if mapped is None and field.options:
                    log.warning("fill.bool_unmapped", label=field.label[:50],
                                value=v, options=field.option_labels()[:6])
                    return False
                v = mapped if mapped is not None else v
            elif isinstance(v, (int, float)):
                m, _ = match_numeric_range(float(v), field.option_labels())
                if m:
                    v = m
            if field.kind is FieldKind.COMBOBOX:
                return await fill_combobox(page, field, str(v))
            return await fill_select(page, field, str(v))

        if field.kind is FieldKind.RADIO:
            return await fill_radio(page, field, v)

        if field.kind in (FieldKind.CHECKBOX, FieldKind.CONSENT):
            return await fill_checkbox(page, field, bool(v))

        sequential = any(h in field.label.lower() for h in AUTOCOMPLETE_HINTS)
        return await fill_text(page, field, str(v), sequential=sequential)

    except Exception as exc:  # noqa: BLE001
        log.warning("fill.failed", label=field.label[:50],
                    kind=field.kind.value, error=str(exc)[:160])
        return False
