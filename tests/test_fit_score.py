"""Fit filtering must not punish a source for the fields it omits."""

from __future__ import annotations

import pathlib

import pytest
import yaml

from jobbot.ats.detect import ATS
from jobbot.discovery.sources import JobPost
from jobbot.orchestrator import fit_score
from jobbot.profile import Profile

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def profile() -> Profile:
    return Profile.model_validate(
        yaml.safe_load((ROOT / "config" / "profile.example.yaml").read_text(encoding="utf-8"))
    )


def _post(**kw) -> JobPost:
    base = dict(ats=ATS.WORKDAY, native_id="1", company="Acme",
                title="Software Engineer", url="https://example.com")
    return JobPost(**{**base, **kw})


def test_posting_without_a_description_is_not_filtered_out(profile: Profile) -> None:
    """Discovery.workday sets description="", and that must not filter.

    Unfixed, the skills term scored 0 against a bare title, every Workday
    posting landed on 0.4, and RunConfig.min_match_score defaults to 0.5.
    """
    assert fit_score(profile, _post(description="")) >= 0.5


def test_a_description_still_drives_the_score(profile: Profile) -> None:
    """The new branch must not flatten scoring where a description exists."""
    matching = _post(ats=ATS.GREENHOUSE,
                     description="We use Python, Go, PostgreSQL, Docker, Redis.")
    unrelated = _post(ats=ATS.GREENHOUSE, title="Veterinary Technician",
                      description="Animal husbandry and clinic scheduling.")
    assert fit_score(profile, matching) > fit_score(profile, unrelated)
    assert fit_score(profile, unrelated) < 0.5
