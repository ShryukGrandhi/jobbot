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
import unicodedata
from typing import Sequence

from rapidfuzz import fuzz

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

_STOP = {"a", "an", "the", "of", "or", "and", "in", "to", "degree", "s"}


def fold_accents(s: str) -> str:
    """Drop combining marks, keep everything else.

    Greenhouse's school search returns nothing for "San José State University"
    and the right answer for "San Jose State University", so this is needed for
    what gets typed as well as for what gets compared.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


# A location dropdown says "Seattle, WA" and the answer we hold says "Seattle,
# Washington, United States". Those scored below every threshold and a required
# field was left empty. Only a TRAILING two-letter code is expanded: "IN", "OR",
# "OK", "ME" and "HI" are ordinary words, and expanding them mid-sentence would
# turn "will you work in office" into "will you work indiana office".
_STATE_CODES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island",
    "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
    "dc": "district of columbia",
}


# A code that follows a comma is a state: "Costa Mesa, CA (HQ)", "Seattle, WA".
# Bare "in"/"or"/"ok" in a sentence never is, and neither is "CA" with no comma
# before it, so the comma is what makes this safe to apply anywhere in a string.
_COMMA_STATE = re.compile(r",\s*([A-Za-z]{2})\b")


def _expand_comma_states(s: str) -> str:
    return _COMMA_STATE.sub(
        lambda m: ", " + _STATE_CODES.get(m.group(1).lower(), m.group(1)), s)


def _expand_trailing_state(words: list[str]) -> list[str]:
    if len(words) >= 2 and words[-1] in _STATE_CODES:
        return words[:-1] + _STATE_CODES[words[-1]].split()
    return words


def normalize(s: str) -> str:
    # The profile says "San José State University" and the picker lists
    # "San Jose State University"; comparing those as different strings left a
    # required field empty.
    s = _expand_comma_states(fold_accents(s))
    s = s.lower().replace("’", "'").replace("‘", "'")
    s = s.replace("'", "")
    s = _PUNCT.sub(" ", s)
    return " ".join(_expand_trailing_state(_WS.sub(" ", s).strip().split()))


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


_NEGATED = re.compile(r"\b(not|no|never|unable|unwilling|decline|don'?t|cannot)\b", re.I)
# A willingness, not an assertion about where someone lives or what they hold.
_NONCOMMITTAL = re.compile(r"\b(willing|able|open to|prepared to|happy to)\b", re.I)


def match_yes_no_prose(value: object, options: Sequence[str]) -> str | None:
    """Map yes/no onto options written as sentences.

    Cloudflare asks "Do you currently live or are you willing to relocate?" and
    offers three sentences; the answer we hold is "Yes", which matches none of
    them, so a required field stayed empty.

    For "no", take the negated option. For "yes", prefer an option stating a
    willingness over one asserting a fact: "I am willing to relocate" is true
    of a candidate open to relocating, while "I currently live in this job's
    location" may not be, and choosing between two factual claims would put a
    statement in the candidate's mouth. With no willingness option and more
    than one affirmative, this declines rather than guess.
    """
    if isinstance(value, bool):
        yes = value
    else:
        nv = normalize(str(value))
        if nv in {normalize(x) for x in _YES}:
            yes = True
        elif nv in {normalize(x) for x in _NO}:
            yes = False
        else:
            return None

    # Only meaningful when the options are prose, not literal yes/no labels.
    if any(normalize(o) in {normalize(w) for w in (*_YES, *_NO)} for o in options):
        return None

    negated = [o for o in options if _NEGATED.search(o)]
    affirmative = [o for o in options if o not in negated]

    if not yes:
        return negated[0] if len(negated) == 1 else None

    willing = [o for o in affirmative if _NONCOMMITTAL.search(o)]
    if len(willing) == 1:
        return willing[0]
    if len(affirmative) == 1:
        return affirmative[0]
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


# Every ATS words "I'd rather not say" differently. A profile that says
# "I do not wish to answer" must still find "Decline To Self Identify",
# otherwise a voluntary EEO field is left blank and the form fails validation.
_DECLINE_PHRASES = (
    "decline to self identify", "decline to self-identify", "decline",
    "i do not wish to answer", "i don't wish to answer", "i dont wish to answer",
    "prefer not to say", "prefer not to answer", "i prefer not to say",
    "choose not to disclose", "do not wish to disclose", "not disclosed",
    "i do not want to answer", "wish not to answer", "no answer",
    "prefer not to disclose", "opt out", "unspecified",
)


def is_decline(value: object) -> bool:
    """True only for a genuine 'prefer not to say'.

    Matching is word-boundary aware and requires a real phrase. A naive
    substring test matches "No" inside "I do NOt wish to answer", which would
    answer an EEO question "No" when the candidate asked to decline -- a false
    statement on a form, not a formatting slip.
    """
    n = normalize(str(value))
    if not n or len(n) < 6:            # "no", "yes" are never a decline
        return False
    words = n.split()
    for phrase in _DECLINE_PHRASES:
        pn = normalize(phrase)
        if not pn:
            continue
        if n == pn:
            return True
        pw = pn.split()
        # phrase must appear as a contiguous run of whole words
        if len(pw) > 1 and len(words) >= len(pw):
            for i in range(len(words) - len(pw) + 1):
                if words[i:i + len(pw)] == pw:
                    return True
        elif len(pw) == 1 and len(pw[0]) > 5 and pw[0] in words:
            return True
    return False


def match_decline(options: Sequence[str]) -> str | None:
    """Find whichever wording this form uses for 'prefer not to say'."""
    for o in options:
        if is_decline(o):
            return o
    return None


_AFFIRM = ("yes", "y", "true", "i agree", "agree", "accept", "i accept",
           "i understand", "acknowledge", "i acknowledge", "confirm", "1")


def is_affirmative(value: object) -> bool:
    if isinstance(value, bool):
        return value
    n = normalize(str(value))
    return n in {normalize(a) for a in _AFFIRM}


def match_acknowledgement(value: object, options: Sequence[str]) -> str | None:
    """Handle consent controls whose only choice is the affirmative statement.

    Arbitration and policy acknowledgements are often a single-option select
    reading "I understand and agree to the terms...". There is no "Yes" to
    match. Selecting it is exactly what a confirmed affirmative in the profile
    authorizes -- and note this returns None for anything that is not an
    explicit affirmative, so a blank or a "No" never silently agrees.
    """
    if len(options) != 1 or not is_affirmative(value):
        return None
    only = options[0]
    n = normalize(only)
    if any(k in n for k in ("agree", "understand", "acknowledge", "read",
                            "accept", "confirm", "consent")):
        return only
    return None
