"""Option text goes into selectors, and employer text has apostrophes in it."""

from __future__ import annotations

import json

import pytest

from jobbot.forms.fill import q


@pytest.mark.parametrize("label", [
    "Bachelor's Degree",
    "I don't wish to answer",
    'He said "yes"',
    "Yes",
])
def test_option_text_round_trips_through_a_selector(label: str) -> None:
    """The quoted form must be a valid string literal AND still mean the label.

    Unfixed, `label:has-text('Bachelor's Degree')` was unparseable; all three
    radio fallbacks threw identically and the answer was dropped.
    """
    quoted = q(label)
    assert quoted.startswith('"') and quoted.endswith('"')
    assert json.loads(quoted) == label
