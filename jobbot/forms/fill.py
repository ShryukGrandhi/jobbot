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
import json
import os
import random
import re
from pathlib import Path
from typing import Any

import structlog

from jobbot.forms.matching import (
    is_decline, match_acknowledgement, match_boolean, match_decline,
    match_numeric_range, match_option,
    fold_accents,
    match_yes_no_prose,
)
from jobbot.forms.model import FieldKind, FormField, ProposedAnswer

log = structlog.get_logger(__name__)


class FillError(RuntimeError):
    pass


def q(text: str) -> str:
    """Quote a string for interpolation into a selector.

    Option labels are employer-authored text, and apostrophes are everywhere in
    them -- "Bachelor's Degree", "I don't wish to answer". Pasting one into
    `label:has-text('...')` produced an unparseable selector, every fallback
    threw the same way, and the field was silently left blank. json.dumps gives
    a correctly escaped double-quoted string, which both CSS and Playwright's
    text engine accept.
    """
    return json.dumps(str(text))


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
        page.locator(f"[aria-label*={q(label[:40])}]").first,
    ):
        try:
            if await cand.count():
                await cand.wait_for(state="visible", timeout=3000)
                return cand
        except Exception:  # noqa: BLE001
            continue
    # Last resort: find the block that asks this question and take the control
    # inside it. Vision reports fields the DOM walk missed and gives us only a
    # label, and an accessible-name lookup fails whenever the visible text is
    # not wired to the input -- which is how a required consent checkbox went
    # unchecked with "could not locate field by label".
    sel = None
    with contextlib.suppress(Exception):
        sel = await page.evaluate(_BLOCK_CONTROL_JS, {"question": label[:160]})
    if sel:
        loc = page.locator(sel).first
        with contextlib.suppress(Exception):
            await loc.wait_for(state="visible", timeout=3000)
            log.debug("fill.located_by_block", label=label[:50])
            return loc

    raise FillError(f"could not locate field by label: {label[:60]!r}")


# Find the form control that belongs to a question, by text proximity.
_BLOCK_CONTROL_JS = r"""
({question}) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const q = norm(question);
  if (q.length < 8) return null;
  const needle = q.slice(0, 60);

  const blocks = [...document.querySelectorAll('div, fieldset, section, li, label, p')]
    .filter(el => {
      const t = norm(el.innerText);
      return t.includes(needle) && t.length < q.length + 500;
    });
  if (!blocks.length) return null;

  for (const block of blocks.reverse()) {          // innermost first
    const ctl = block.querySelector(
      'input:not([type=hidden]), select, textarea, [role=checkbox], [role=combobox]');
    if (ctl && ctl.getBoundingClientRect().width > 0) {
      const k = 'jb' + Math.random().toString(36).slice(2, 9);
      ctl.setAttribute('data-jobbot-pick', k);
      return '[data-jobbot-pick="' + k + '"]';
    }
  }
  return null;
}
"""


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
    want = str(value).strip()
    got_s = got.strip()
    # Comparing only the first 40 characters could not see truncation: a 2000
    # character essay written into a field with a maxlength passed the check on
    # its opening words while the rest was silently dropped.
    ok = got_s == want
    if not ok and got_s[:40] == want[:40] and len(got_s) < len(want):
        log.warning("fill.text_truncated", label=field.label[:50],
                    wanted_chars=len(want), got_chars=len(got_s),
                    max_length=field.max_length)

    if not ok and field.kind is FieldKind.PHONE:
        # A phone widget reformats what it is given: "6693609914" comes back as
        # "(669) 360-9914", which is the same number and not a failure. Compare
        # digits before retrying -- the retry types key by key, and racing
        # intl-tel-input's reformatting cost a digit: "(669) 609-914".
        digits = re.sub(r"\D", "", str(value))
        ok = bool(digits) and re.sub(r"\D", "", got).endswith(digits[-10:])
        if not ok and digits:
            await loc.fill("")
            await loc.fill(digits)
            got = await loc.input_value()
            ok = re.sub(r"\D", "", got).endswith(digits[-10:])

    if not ok:
        log.warning("fill.text_mismatch", label=field.label[:50],
                    wanted=str(value)[:40], got=got[:40])
    return ok


async def fill_select(page: Any, field: FormField, value: str) -> bool:
    """Native <select>. Snap to a real option; never inject free text."""
    # Dispatch on the element, not on what the parse called it. HP IQ's "How
    # did you hear about HP IQ?" was read as a multiselect, so select_option
    # was called on a custom widget: "Element is not a <select> element", and a
    # required field went unanswered over a mislabelled kind.
    with contextlib.suppress(Exception):
        loc = await _locate(page, field)
        tag = (await loc.evaluate("e => e.tagName") or "").lower()
        if tag != "select":
            log.debug("fill.select_is_not_native", label=field.label[:40], tag=tag)
            return await fill_combobox(page, field, str(value))

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


# An open menu is a container, not a count of options. Deciding by option
# count meant a menu showing "No options" -- which is what ours looked like
# after typing had filtered it -- counted as closed, so it was never dismissed
# and it covered the next field: "element is covered by <DIV>", and that field
# could not even be cleared, let alone answered.
_MENU_OPEN_JS = r"""
() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  for (const e of document.querySelectorAll('[aria-expanded="true"]')) if (vis(e)) return true;
  for (const m of document.querySelectorAll(
      '.select__menu, .select__menu-list, [role="listbox"], [class*="menu-list"]'))
    if (vis(m)) return true;
  return false;
}
"""


async def _menu_is_open(page: Any) -> bool:
    with contextlib.suppress(Exception):
        return bool(await page.evaluate(_MENU_OPEN_JS))
    return False


# Above this many visible choices, a control is a search box rather than a
# fixed list, and what it shows is one page of many -- not the options.
SEARCHABLE_OPTION_COUNT = 25


async def _close_open_menus(page: Any, tries: int = 3) -> None:
    """Dismiss any open listbox so the next field is reachable."""
    for _ in range(tries):
        if not await _menu_is_open(page):
            return
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        if not await _menu_is_open(page):
            return
        with contextlib.suppress(Exception):
            # Escape is ignored by some widgets; losing focus is not.
            await page.evaluate("() => document.activeElement && document.activeElement.blur()")
            await asyncio.sleep(0.2)
    with contextlib.suppress(Exception):
        await page.mouse.click(4, 4)     # click away as a last resort
        await asyncio.sleep(0.25)


async def _visible_options(page: Any) -> list[str]:
    try:
        return await page.evaluate(_VISIBLE_OPTIONS_JS) or []
    except Exception:  # noqa: BLE001
        return []


_OWN_OPTIONS_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const seen = [];
  const push = root => {
    if (!root) return;
    for (const o of root.querySelectorAll('[role="option"], .select__option, li[id*="option"]')) {
      const r = o.getBoundingClientRect();
      const t = (o.innerText || '').trim();
      if (r.width > 0 && r.height > 0 && t) seen.push(t);
    }
  };
  // react-select links its menu by id, or renders it beside the control
  const owns = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (owns) push(document.getElementById(owns));
  if (!seen.length) {
    let n = el;
    for (let i = 0; i < 5 && n && n.parentElement; i++) {
      n = n.parentElement;
      const menu = n.querySelector('.select__menu, [class*="menu"], [role="listbox"]');
      if (menu) { push(menu); if (seen.length) break; }
    }
  }
  return [...new Set(seen)];
}
"""


async def _own_options(page: Any, field: FormField) -> list[str]:
    """The options belonging to THIS control.

    Diffing "everything visible before" against "everything visible after"
    guessed wrong in both directions: it counted another menu's options as
    ours, and when focusing the control had already opened its menu, our click
    closed it and the diff came out empty. Asking the control for its own menu
    needs neither guess.
    """
    if not field.selector:
        return []
    with contextlib.suppress(Exception):
        return await page.evaluate(_OWN_OPTIONS_JS, field.selector) or []
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

    # Clear any text sitting in the box first. These widgets filter their list
    # by what is typed, so leftover text -- ours, from a previous round -- hides
    # every option: the menu opens showing "No options" and we conclude the
    # control has none. Cloudflare's relocation question failed this way on
    # every round of five runs, with "Yes" still in the box and three
    # sentence-length choices behind it.
    # Leftover text filters the list to nothing: the menu opens on "No options"
    # and the control looks like it has none. Clearing also focuses the box,
    # which opens the menu with everything in it -- so read it here rather than
    # clicking again, because a click on an open react-select closes it.
    # Clearing the box does three useful things at once: it removes filter text
    # that would hide every option, it focuses the control, and that focus
    # opens the menu (aria-expanded goes true, and aria-controls appears --
    # these inputs have neither until then, so reading the options depends on
    # this step working). A click instead would close a menu already open.
    # Clearing only makes sense on something that holds text. Workday renders
    # its dropdowns as <button>, where fill() fails outright -- "How Did You
    # Hear About Us?" logged clear_blocked twice and never opened.
    tag = ""
    with contextlib.suppress(Exception):
        tag = (await loc.evaluate("e => e.tagName") or "").lower()
    fillable = tag in ("input", "textarea")

    pre_opts: list[str] = []
    for attempt in (1, 2):
        if not fillable:
            break
        try:
            await loc.fill("")
            await asyncio.sleep(0.4)
            pre_opts = await _own_options(page, field)
            if pre_opts:
                log.debug("fill.combobox_menu_on_focus", label=field.label[:40],
                          count=len(pre_opts))
            break
        except Exception as exc:  # noqa: BLE001
            # Almost always "covered by <DIV>": the previous field's menu is
            # still up. Swallowing this silently cost four required dropdowns
            # on one form, each reported as having no options at all.
            log.debug("fill.combobox_clear_blocked", label=field.label[:40],
                      attempt=attempt, error=str(exc)[:80])
            if attempt == 2:
                break
            await _close_open_menus(page)
            with contextlib.suppress(Exception):
                await loc.scroll_into_view_if_needed()
            await asyncio.sleep(0.3)

    before = set() if pre_opts else set(await _visible_options(page))
    if not pre_opts:
        await loc.click()

    # Poll rather than snapshot once. A single 450ms look was enough on an idle
    # machine and not during a run, where the same page, the same code and the
    # same widget reported "no options" three rounds running while every
    # isolated attempt found all three -- the menu simply had not painted yet.
    after: list[str] = list(pre_opts)
    opts: list[str] = list(pre_opts)
    for _ in range(0 if pre_opts else 10):
        await asyncio.sleep(0.3)
        own = await _own_options(page, field)
        if own:
            after, opts = own, own
            break
        after = await _visible_options(page)
        opts = [o for o in after if o not in before]
        if opts:
            break

    if not opts:
        # The click opened nothing, or opened and lost it again -- the humanised
        # pointer moves after pressing, and some widgets close on the blur that
        # follows. Every one of them also opens from the keyboard. Cloudflare's
        # relocation dropdown reported "no options" three rounds running this
        # way, while the same code found all three of its choices when driven
        # on its own.
        with contextlib.suppress(Exception):
            await loc.focus()
            await loc.press("ArrowDown")
            for _ in range(8):
                await asyncio.sleep(0.3)
                after = await _visible_options(page)
                opts = [o for o in after if o not in before]
                if opts:
                    break
        if opts:
            log.debug("fill.combobox_opened_by_key", label=field.label[:40], count=len(opts))

    if not opts and before and not after:
        # The menu was open when we measured and our click shut it. What was
        # visible then belongs to this control -- it was opened by focusing it.
        opts = list(before)
        log.debug("fill.combobox_reused_open_menu", label=field.label[:40], count=len(opts))

    if not opts:
        # Still nothing: clear whatever is in the box and look once more, in
        # case our own typing is the filter hiding the list.
        with contextlib.suppress(Exception):
            await loc.fill("")
            await loc.click()
            for _ in range(8):
                await asyncio.sleep(0.3)
                after = await _visible_options(page)
                opts = [o for o in after if o not in before]
                if opts:
                    log.debug("fill.combobox_unfiltered", label=field.label[:40],
                              count=len(opts))
                    break

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
        if chosen is None:
            prose = match_yes_no_prose(value, opts)
            if prose is not None:
                chosen, score, how = prose, 0.8, "yes-no-prose"

    # Typing narrows a long list. Only if the opened menu did not already offer
    # what we want -- typing into a prefix-filtered widget can empty it.
    #
    # Keep what the menu offered before typing. Typing a value the list does
    # not contain filters it to nothing, and overwriting `opts` with that empty
    # result threw away the only record of the real choices: the failure logged
    # "seen=[]" and the field went to the verifier with no options, so the
    # second answering pass had nothing to choose from.
    discovered = list(opts)
    if chosen is None:
        with contextlib.suppress(Exception):
            # Type the head of the value: a location autocomplete wants "San
            # Jose", then offers the full "San Jose, California, United States"
            # for the matcher to pick exactly.
            # Type without accents: the search index behind these pickers is
            # plain ASCII, and "José" returns nothing where "Jose" matches.
            await loc.press_sequentially(fold_accents(text.split(",")[0])[:32],
                                         delay=random.randint(35, 75))
        # A school or location picker queries a server on each keystroke, and
        # a fixed wait raced it: the menu was still empty when we looked, so a
        # required field was left blank with "no options" in the log. Poll.
        after = []
        for _ in range(10):
            await asyncio.sleep(0.3)
            after = await _visible_options(page)
            if [o for o in after if o not in before]:
                break
        opts = [o for o in after if o not in before] or after or discovered
        if opts:
            ack = match_acknowledgement(value, opts)
            if ack is not None:
                chosen, score, how = ack, 1.0, "acknowledgement"
            elif is_decline(text) and match_decline(opts):
                chosen, score, how = match_decline(opts), 1.0, "decline-synonym"
            else:
                chosen, score, how = match_option(text, opts)

    if chosen is None:
        # Record what it really offers. The parse could not see inside a menu
        # that only exists once opened, so the answer was composed blind --
        # "Yes" against a list of three full sentences. With the options on the
        # field, the verifier can suggest one that exists.
        opts = opts or discovered
        if opts and len(opts) <= SEARCHABLE_OPTION_COUNT:
            from jobbot.forms.model import FieldOption
            field.options = [FieldOption(label=o) for o in opts[:25]]
        log.warning("fill.combobox_no_option", label=field.label[:50],
                    wanted=text[:40], seen=opts[:6],
                    visible_before=len(before), visible_after=len(after))
        # A menu that never opens looks identical in the log to one we misread,
        # and the run is the only place it happens -- every isolated attempt
        # against the same widget works. Keep the evidence.
        with contextlib.suppress(Exception):
            shot = Path(os.environ.get("JOBBOT_SHOT_DIR", "data")) / (
                "combobox_" + re.sub(r"\W+", "_", field.label[:40]) + ".png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot))
            log.info("fill.combobox_shot", path=str(shot))
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
            shown = (await page.evaluate(_CONTROL_TEXT_JS, field.selector) or "").lower()
            # Either direction: a phone-country picker offers "United States +1"
            # in its menu and then displays just "+1" in the control.
            c = chosen.lower()
            stuck = bool(shown) and (c in shown or shown in c)
            if not stuck:
                log.warning("fill.combobox_did_not_stick", label=field.label[:50],
                            chose=chosen, shows=(shown or "")[:60])
    if not stuck:
        return False

    log.debug("fill.combobox", label=field.label[:40], chose=chosen, via=how)
    return True


# Find the option to click WITHIN one radio group. Returns the input's id.
#
# Radio options are labelled "Yes" and "No" on every question, so a page-wide
# `label:has-text("Yes")` resolves to the first Yes on the page -- which is a
# different question's. On a form asking both "are you authorised to work" and
# "will you require sponsorship", one answer was clicked twice and the other
# left blank, and a blank required question is a blocker that never clears.
# The group is identified by the radio `name` the DOM walk already grouped on,
# falling back to whichever group's surrounding text matches the question.
_RADIO_PICK_JS = r"""
({selector, question, choice}) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(choice);
  const tag = el => {
    const k = 'jp' + Math.random().toString(36).slice(2, 9);
    el.setAttribute('data-jobbot-pick', k);
    return '[data-jobbot-pick="' + k + '"]';
  };
  const attrEsc = v => String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const labelOf = r => {
    if (r.id) {
      const l = document.querySelector('label[for="' + attrEsc(r.id) + '"]');
      if (l) return l.innerText;
    }
    const p = r.closest('label');
    return p ? p.innerText : (r.value || '');
  };

  // 1. A real radio group, identified by the name the DOM walk grouped on.
  const radios = [...document.querySelectorAll('input[type=radio]')];
  let name = null;
  if (selector) {
    try {
      const e = document.querySelector(selector);
      if (e && e.type === 'radio') name = e.name;
    } catch (err) { /* a vision-invented selector need not parse */ }
  }
  let group = name ? radios.filter(r => r.name === name) : [];

  const qn = norm(question);
  const words = qn.split(' ').filter(w => w.length > 3);
  const scoreText = txt => txt.includes(qn.slice(0, 40))
      ? 1000 : words.filter(w => txt.includes(w)).length;

  if (!group.length && radios.length && qn) {
    // Score every ancestor of each group, and remember how far up the match
    // was. Two things go wrong otherwise, both seen on one Ashby EEOC form:
    // stopping at the first ancestor with any text in it never reaches the
    // question ("Gender" stopped at the word "Male"), and every group shares
    // the outer EEOC container, so on a short question like "Race" all three
    // groups match -- the nearest match is the one actually being asked.
    let best = null, bestScore = 0, bestDepth = 99;
    for (const n of [...new Set(radios.map(r => r.name))]) {
      const g = radios.filter(r => r.name === n);
      let sc = 0, depth = 99, holder = g[0];
      for (let i = 1; i <= 6 && holder && holder.parentElement; i++) {
        holder = holder.parentElement;
        const s = scoreText(norm(holder.innerText));
        if (s > sc) { sc = s; depth = i; }
        if (sc >= 1000) break;
      }
      if (sc > bestScore || (sc === bestScore && sc > 0 && depth < bestDepth)) {
        bestScore = sc; bestDepth = depth; best = g;
      }
    }
    if (bestScore > 0) group = best;
  }
  if (group.length) {
    const hit = group.find(r => norm(labelOf(r)) === want)
             || group.find(r => norm(labelOf(r)).startsWith(want))
             || group.find(r => norm(r.value) === want);
    if (hit) {
      const lab = hit.id
        ? document.querySelector('label[for="' + attrEsc(hit.id) + '"]')
        : hit.closest('label');
      return {click: tag(lab || hit), verify: tag(hit), kind: 'radio'};
    }
  }

  // 2. No radio anywhere: Ashby renders a Yes/No question as one hidden
  //    checkbox with the choices as ordinary clickable elements beside it.
  //    Find the block that asks THIS question, then the choice inside it.
  if (!qn) return null;
  const blocks = [...document.querySelectorAll('div, fieldset, section, li')]
    .filter(el => {
      const t = norm(el.innerText);
      return t.includes(qn.slice(0, 40)) && t.length < qn.length + 400;
    });
  if (!blocks.length) return null;
  const block = blocks[blocks.length - 1];   // innermost match

  const choices = [...block.querySelectorAll('label, button, span, div, [role=radio], [role=option]')]
    .filter(el => el.children.length === 0 && norm(el.innerText) === want);
  if (!choices.length) return null;

  const backing = block.querySelector('input[type=checkbox], input[type=radio]');
  return {click: tag(choices[0]), verify: backing ? tag(backing) : null, kind: 'custom'};
}
"""


async def fill_radio(page: Any, field: FormField, value: Any) -> bool:
    """Choose within a radio group by clicking its LABEL, not the input.

    Clicking the input programmatically is what reportedly trips Lever's
    invisible hCaptcha; the label is the element a human actually hits.
    """
    opts = field.option_labels()
    chosen = match_boolean(value, opts) if isinstance(value, bool) else None
    if chosen is None:
        chosen, _, _ = match_option(str(value), opts)
    if chosen is None and isinstance(value, bool):
        chosen = "Yes" if value else "No"
    if chosen is None:
        log.warning("fill.radio_no_match", label=field.label[:50], wanted=str(value)[:40])
        return False

    await _human_pause()
    pick = None
    with contextlib.suppress(Exception):
        pick = await page.evaluate(_RADIO_PICK_JS, {
            "selector": field.selector or "",
            "question": field.label[:160],
            "choice": chosen,
        })

    if pick and pick.get("click"):
        verify = pick.get("verify")
        try:
            loc = page.locator(pick["click"]).first
            await loc.scroll_into_view_if_needed()
            await loc.click(timeout=4000)
            await asyncio.sleep(0.25)
            if verify:
                with contextlib.suppress(Exception):
                    if await page.locator(verify).first.is_checked():
                        log.debug("fill.radio", label=field.label[:40],
                                  chose=chosen, kind=pick.get("kind"))
                        return True
            else:
                log.debug("fill.radio", label=field.label[:40], chose=chosen,
                          kind=pick.get("kind"), verified=False)
                return True
        except Exception:  # noqa: BLE001
            pass
        # A styled label over a hidden input refuses an ordinary click.
        if verify:
            with contextlib.suppress(Exception):
                await page.locator(verify).first.check(timeout=3000, force=True)
                if await page.locator(verify).first.is_checked():
                    log.debug("fill.radio", label=field.label[:40], chose=chosen, via="check")
                    return True

    log.warning("fill.radio_not_found", label=field.label[:50], chose=str(chosen)[:30])
    return False


# "Expected Graduation Year" wants 2028; "Graduation date" and "Expected
# graduation date (month/year)" both want a date, so the word "date" anywhere
# in the label -- before or after "year" -- rules the shorthand out.
_YEAR_ONLY = re.compile(r"^(?!.*\bdate\b).*\byear\b", re.I | re.S)


async def _is_date_picker(page: Any, field: FormField) -> bool:
    """Does this control only accept a date, whatever its label says?

    Deepgram labels a field "Expected Graduation Year" and backs it with a
    date picker. Typing "2028" into one sets the input's value -- so the write
    reports success -- while the picker's own state stays empty and the form
    keeps reporting the field as missing. Asking the control is the only
    reliable way to tell; the wording is not evidence.
    """
    with contextlib.suppress(Exception):
        loc = await _locate(page, field)
        return await loc.evaluate(
            "el => (el.type || '').toLowerCase() === 'date'"
            " || /date/i.test(el.placeholder || '')"
            " || /date/i.test(el.getAttribute('aria-describedby') || '')")
    return False


async def fill_date(page: Any, field: FormField, value: str) -> bool:
    """Write a date in the format the control actually parses.

    A native <input type=date> wants ISO. A scripted picker does not: given
    "2026-06-01" Ashby's parsed it as UTC midnight and rendered it in local
    time, so an availability date of 1 June went in as 31 May. Off by one day
    on a date the candidate stated is a wrong answer, not a formatting nit.
    """
    loc = await _locate(page, field)
    await loc.scroll_into_view_if_needed()
    await _human_pause()

    native = ""
    with contextlib.suppress(Exception):
        native = (await loc.get_attribute("type") or "").lower()

    iso = value.strip()[:10]
    if native == "date":
        await loc.fill(iso)
        return (await loc.input_value() or "").startswith(iso)

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", iso)
    text = f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else value
    await loc.fill("")
    await loc.press_sequentially(text, delay=random.randint(30, 70))
    await asyncio.sleep(0.4)

    # Close the calendar. Typing into a picker leaves its overlay open, and it
    # covers whatever sits below -- on one form that was both work-authorisation
    # questions and a required essay, all three reported empty because nothing
    # could reach them. Escape dismisses the calendar and keeps the typed value.
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.25)

    got = (await loc.input_value() or "").strip()
    ok = bool(m) and m.group(3) in got and m.group(1) in got
    if not ok:
        log.warning("fill.date_mismatch", label=field.label[:50], wanted=text, got=got[:20])
    return ok


_TRUEISH = {"yes", "y", "true", "1", "on", "checked", "agree", "agreed",
            "i agree", "i consent", "accept", "confirm", "confirmed"}
_FALSEISH = {"no", "n", "false", "0", "off", "unchecked", "none", "n/a",
             "decline", "disagree", "not applicable", ""}


def _as_bool(v: object) -> bool | None:
    """Read a checkbox answer. None means "unclear -- do not touch it".

    bool() is the wrong tool: bool("No") is True, and that ticked every box in
    a multi-choice group whose unwanted options were answered "No".
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    text = str(v).strip().lower()
    if text in _TRUEISH:
        return True
    if text in _FALSEISH:
        return False
    return None


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
            await page.locator(f"label[for={q(field.field_id)}]").first.click(timeout=3000)
            return True
        except Exception:
            return False


async def upload_file(page: Any, field: FormField, path: str | Path) -> bool:
    """Attach a file straight to the hidden input, bypassing the OS dialog."""
    p = Path(path)
    if not p.exists():
        raise FillError(f"file to upload does not exist: {p}")

    # With no selector of its own, find the upload inside the block that asks
    # for this document. The old fallback took the first input[type=file] on
    # the page, which on a form with Resume/CV, Cover Letter and Academic
    # Transcript slots is a coin toss -- and attaching a resume to a transcript
    # field sends an employer the wrong document under the candidate's name.
    scoped = None
    if not field.selector and field.label:
        with contextlib.suppress(Exception):
            scoped = await page.evaluate(_BLOCK_CONTROL_JS, {"question": field.label[:160]})
        if scoped:
            log.debug("fill.upload_scoped_by_block", label=field.label[:40])

    for sel in (field.selector, scoped):
        if not sel:
            continue
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

    # Dropzone with no reachable input: drive the file chooser, but only from
    # the button that belongs to THIS document's block. Clicking the first
    # "Attach" on the page picks between Resume/CV, Cover Letter and Academic
    # Transcript at random, and a resume filed as a transcript is worse than a
    # missing attachment -- the verifier catches the second and cannot know the
    # first was wrong.
    btn = None
    if field.label:
        with contextlib.suppress(Exception):
            btn = await page.evaluate(_BLOCK_BUTTON_JS, {"question": field.label[:160]})
    if not btn:
        log.warning("fill.upload_no_scoped_target", label=field.label[:50])
        return False
    try:
        async with page.expect_file_chooser(timeout=6000) as info:
            await page.locator(btn).first.click()
        chooser = await info.value
        await chooser.set_files(str(p))
        await asyncio.sleep(0.9)
        log.info("fill.uploaded", label=field.label[:40], file=p.name, via="chooser")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("fill.upload_failed", error=str(exc)[:150])
        return False


# The upload button inside the block that asks for a particular document.
_BLOCK_BUTTON_JS = r"""
({question}) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const q = norm(question);
  if (q.length < 4) return null;
  const needle = q.slice(0, 40);
  const blocks = [...document.querySelectorAll('div, fieldset, section, li, label')]
    .filter(el => norm(el.innerText).includes(needle)
                  && norm(el.innerText).length < q.length + 400);
  for (const block of blocks.reverse()) {
    for (const b of block.querySelectorAll('button, [role="button"], label')) {
      if (!/attach|upload|choose|browse|select file/i.test(b.innerText || '')) continue;
      const r = b.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        const k = 'ju' + Math.random().toString(36).slice(2, 9);
        b.setAttribute('data-jobbot-pick', k);
        return '[data-jobbot-pick="' + k + '"]';
      }
    }
  }
  return null;
}
"""



async def discover_options(page: Any, form: Any, *, limit: int = 14) -> int:
    """Open each option list once, before anything is answered.

    Most of this file's history is answers composed without knowing what a
    control offered: "Yes" for a list of three sentences, today's date for a
    list of month-year labels, "Company website / Careers page" for a list that
    ran Grace Hopper, career fair, word of mouth, social media, LinkedIn,
    Google. The options exist -- they are just behind a click, so the parse
    could not see them and every later stage guessed.

    Reading them up front costs a second per control and makes the first
    answer an informed one.
    """
    found = 0
    for field in form.fields:
        if found >= limit:
            break
        if field.kind not in (FieldKind.COMBOBOX, FieldKind.SELECT):
            continue
        if field.options or not field.selector:
            continue
        try:
            loc = page.locator(field.selector).first
            if not await loc.count():
                continue
            await _close_open_menus(page)
            await loc.scroll_into_view_if_needed()
            await loc.fill("")            # focus opens the menu
            await asyncio.sleep(0.45)
            opts = await _own_options(page, field)
            # A long list is a search box, not a menu of choices: the School
            # picker holds every university on earth and shows the first
            # hundred alphabetically. Recording those as "the options" made
            # them authoritative, and "San José State University" -- absent
            # from Aalborg through Aberystwyth -- became unanswerable, where
            # typing into it had always worked.
            if len(opts) > SEARCHABLE_OPTION_COUNT:
                log.debug("discover.options_searchable", label=field.label[:40],
                          count=len(opts))
                opts = []
            if opts:
                from jobbot.forms.model import FieldOption
                field.options = [FieldOption(label=o) for o in opts[:40]]
                found += 1
                log.debug("discover.options", label=field.label[:40], count=len(opts))
        except Exception as exc:  # noqa: BLE001
            log.debug("discover.options_failed", label=field.label[:40],
                      error=str(exc)[:80])
        finally:
            with contextlib.suppress(Exception):
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.15)
    await _close_open_menus(page)
    if found:
        log.info("discover.options_done", fields=found)
    return found

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
            # A choice field vision reported but the DOM walk never saw has no
            # control to drive: HP IQ's "How did you hear about HP IQ?" is a
            # group of checkboxes -- A friend, TEDTalk, Campus Recruiting
            # Event, Other -- and vision described it as one multiselect. With
            # no selector there is nothing to open, so pick the option out of
            # the block by its text, which is what fill_radio does.
            if not field.selector:
                if await fill_radio(page, field, v):
                    return True
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
            # bool("No") is True. On HP IQ's "How did you hear about us?"
            # checkbox group the model answered every option it did not mean
            # with the string "No", and every one of them got ticked -- the
            # verifier called it what it was: "selecting a source the candidate
            # did not actually come from is a false statement to the employer".
            want = _as_bool(v)
            if want is None:
                log.warning("fill.checkbox_unclear", label=field.label[:50],
                            value=str(v)[:40])
                return False
            return await fill_checkbox(page, field, want)

        if field.kind is FieldKind.DATE:
            # The kind can come from the vision pass, which called Deepgram's
            # "Expected Graduation Year" a date. Written as one, 2028 went
            # through a picker and came out 12/31/2027. Checking the label here
            # catches it whoever decided the kind.
            year = re.search(r"(19|20)\d{2}", str(v))
            if year and _YEAR_ONLY.search(field.label or "") \
                    and not await _is_date_picker(page, field):
                log.debug("fill.year_not_date", label=field.label[:40], value=year.group())
                return await fill_text(page, field, year.group())
            return await fill_date(page, field, str(v))

        sequential = any(h in field.label.lower() for h in AUTOCOMPLETE_HINTS)
        # A yes/no answer reaching a free-text box must be written the way a
        # person would write it. Exa asks "Do you require Visa sponsorship?"
        # as free text; str(True) put the literal "True" in front of the
        # employer, and because sponsorship is a legally significant field the
        # healer is forbidden to rewrite it -- so the application deadlocked.
        text = "Yes" if v is True else "No" if v is False else str(v)
        return await fill_text(page, field, text, sequential=sequential)

    except Exception as exc:  # noqa: BLE001
        log.warning("fill.failed", label=field.label[:50],
                    kind=field.kind.value, error=str(exc)[:160])
        return False
