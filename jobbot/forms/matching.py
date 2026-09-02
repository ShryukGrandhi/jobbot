"""Snap a proposed value onto a real option.

A model asked for a dropdown value will happily write "Bachelor's degree" when
the only option is "Bachelors Degree (BA/BS)". Submitting free text into a
<select> either throws or silently selects nothing, and a silently-empty
required field fails validation at submit -- after the expensive work is done.

Four passes, cheapest first, mirroring the approach that works well in prior
art: exact -> containment -> stem -> token overlap. Anything below threshold
fails closed and is escalated rather than guessed.
"""

from __future__ import annotations

import re
from typing import Sequence

from rapidfuzz import fuzz

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

_STOP = {"a", "an", "the", "of", "or", "and", "in", "to", "degree", "s"}


def normalize(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("‘", "'")
    s = s.replace("'", "")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _stem(w: str) -> str:
    for suf in ("ing", "ers", "er", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _tokens(s: str) -> set[str]:
    return {_stem(w) for w in normalize(s).split() if w and w not in _STOP}


def match_option(
    value: str,
    options: Sequence[str],
    *,
    threshold: float = 0.5,
) -> tuple[str | None, float, str]:
    """Return (matched_option, score, method). `None` means fail closed."""
    if not options:
        return None, 0.0, "no-options"
    if value is None:
        return None, 0.0, "no-value"

    nv = normalize(str(value))
    norm = [(o, normalize(o)) for o in options]

    # 1. exact, punctuation- and case-insensitive
    for orig, no in norm:
        if no == nv:
            return orig, 1.0, "exact"

    if not nv:
        return None, 0.0, "empty-value"

    # 2. containment, both directions, guarded against trivially short matches
    best: tuple[str, float] | None = None
    for orig, no in norm:
        if len(nv) >= 3 and len(no) >= 3 and (nv in no or no in nv):
            ratio = min(len(nv), len(no)) / max(len(nv), len(no))
            if best is None or ratio > best[1]:
                best = (orig, ratio)
    if best and best[1] >= 0.34:
        return best[0], round(0.6 + 0.3 * best[1], 3), "containment"

    # 3. stemmed token equality
    tv = _tokens(str(value))
    if tv:
        for orig, _ in norm:
            if _tokens(orig) == tv:
                return orig, 0.9, "stem"

    # 4. token overlap (Jaccard), then a fuzz tiebreak
    scored: list[tuple[str, float]] = []
    for orig, _ in norm:
        to = _tokens(orig)
        if not to or not tv:
            continue
        jac = len(tv & to) / len(tv | to)
        scored.append((orig, jac))
    if scored:
        scored.sort(key=lambda x: x[1], reverse=True)
        top, jac = scored[0]
        if jac >= threshold:
            return top, round(jac, 3), "jaccard"

    ratio = max(((o, fuzz.token_sort_ratio(nv, n) / 100.0) for o, n in norm),
                key=lambda x: x[1])
    if ratio[1] >= 0.85:
        return ratio[0], round(ratio[1], 3), "fuzz"

    return None, round(ratio[1], 3), "no-match"


_YES = {"yes", "y", "true", "1", "i am", "i do", "authorized", "agree", "accept"}
_NO = {"no", "n", "false", "0", "i am not", "i do not", "decline"}


def match_boolean(value: object, options: Sequence[str]) -> str | None:
    """Map a boolean onto whatever this form calls yes and no."""
    if isinstance(value, bool):
        want = _YES if value else _NO
    else:
        nv = normalize(str(value))
        if nv in {normalize(x) for x in _YES}:
            want = _YES
        elif nv in {normalize(x) for x in _NO}:
            want = _NO
        else:
            return None
    for o in options:
        if normalize(o) in {normalize(w) for w in want}:
            return o
    for o in options:
        no = normalize(o)
        if any(no.startswith(normalize(w)) for w in want):
            return o
    return None


_RANGE_PATTERNS = [
    # "5-7", "5 - 7", "5 to 7"
    (re.compile(r"(\d+)\s*(?:-|–|to)\s*(\d+)"), "range"),
    # "8+", "10 or more", "at least 3"
    (re.compile(r"(\d+)\s*\+"), "min"),
    (re.compile(r"(\d+)\s*or more"), "min"),
    (re.compile(r"at least\s*(\d+)"), "min"),
    (re.compile(r"more than\s*(\d+)"), "min"),
    # "less than 2", "under 1"
    (re.compile(r"(?:less than|under|fewer than)\s*(\d+)"), "max"),
    # bare "3", "3 years"
    (re.compile(r"^\D*(\d+)\D*$"), "exact"),
]


def match_numeric_range(value: float, options: Sequence[str]) -> tuple[str | None, str]:
    """Place a number into the bucket a dropdown actually offers.

    Years-of-experience is asked as a bucketed dropdown far more often than as a
    number, and it is knockout-eligible -- a single-select CAN auto-reject where
    a free-text field cannot. Picking the wrong bucket here is a silent
    rejection, so an unplaceable value fails closed.
    """
    if not options:
        return None, "no-options"

    parsed: list[tuple[str, str, tuple[float, ...]]] = []
    for o in options:
        # Lowercase only -- normalize() strips punctuation, which would eat the
        # hyphen in "0-1 years" and make every range look like a bare number.
        low = (o or "").lower().strip()
        if normalize(o) in {"none", "no experience", "0"}:
            parsed.append((o, "range", (0.0, 0.0)))
            continue
        for pat, kind in _RANGE_PATTERNS:
            m = pat.search(low)
            if m:
                nums = tuple(float(g) for g in m.groups())
                parsed.append((o, kind, nums))
                break

    # An exact bucket match wins outright.
    for o, kind, nums in parsed:
        if kind == "range" and nums[0] <= value <= nums[1]:
            return o, "in-range"
    for o, kind, nums in parsed:
        if kind == "min" and value >= nums[0]:
            return o, "at-least"
    for o, kind, nums in parsed:
        if kind == "max" and value < nums[0]:
            return o, "under"
    for o, kind, nums in parsed:
        if kind == "exact" and abs(nums[0] - value) < 0.5:
            return o, "exact"

    # Otherwise take the closest bucket that does not overstate experience:
    # claiming more than you have is a false statement, claiming less is merely
    # conservative. Bias downward on ties.
    best: tuple[str, float] | None = None
    for o, kind, nums in parsed:
        anchor = nums[0]
        if anchor > value:
            continue
        dist = value - anchor
        if best is None or dist < best[1]:
            best = (o, dist)
    if best:
        return best[0], "nearest-not-over"
    return None, "no-match"
