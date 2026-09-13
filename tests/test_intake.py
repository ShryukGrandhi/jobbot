"""Merging an AI proposal into the profile.

`apply_patches` is the only place where model output reaches the file that
speaks in the user's name, so the things it must never do get pinned here:
duplicate a job, lose a bullet, or touch a screening answer.
"""

from __future__ import annotations

from jobbot.intake import apply_patches, to_cards


def test_same_role_from_two_resumes_merges_instead_of_duplicating() -> None:
    """Tailored resumes describe one career; accepting both is the normal path."""
    raw = {"experience": [{
        "company": "Example Corp", "title": "Software Engineer",
        "bullets": ["Rebuilt the ingestion path, p99 840ms -> 95ms"],
        "tech": ["Go"],
    }]}
    out = apply_patches(raw, [{"experience_add": {
        "company": "example corp", "title": "Software Engineer",   # case differs
        "bullets": ["Rebuilt the ingestion path, p99 840ms -> 95ms",   # dupe
                    "Migrated 38 services with no downtime"],          # new
        "tech": ["Go", "Kafka"],
    }}])
    assert len(out["experience"]) == 1, "one job, not one per resume"
    role = out["experience"][0]
    assert len(role["bullets"]) == 2, "the new bullet is kept, the repeat is not"
    assert role["tech"] == ["Go", "Kafka"]


def test_intake_never_writes_a_screening_answer() -> None:
    """The one thing the model is not allowed to decide."""
    raw = {"screening": {"work_authorization": {"value": True, "provenance": "confirmed"}}}
    out = apply_patches(raw, [
        {"screening": {"criminal_history": False}},          # if it ever tried
        {"screening_add": ["anything"]},
        {"experience_add": {"company": "C", "title": "T"}},
    ])
    assert out["screening"] == raw["screening"], \
        "screening comes only from the user choosing in the editor"


def test_lists_dedupe_case_insensitively() -> None:
    raw = {"languages": ["English"], "awards": []}
    out = apply_patches(raw, [
        {"languages_add": ["english", "Urdu"]},
        {"awards_add": ["ICPC Regional 2nd place"]},
    ])
    assert out["languages"] == ["English", "Urdu"]
    assert out["awards"] == ["ICPC Regional 2nd place"]


def test_skills_and_extra_survive_the_storage_shape() -> None:
    """On disk these are maps; the patch carries rows. Both must land."""
    raw = {"skills": {"Languages": ["Python"]}, "extra": {"Note": "old"}}
    out = apply_patches(raw, [
        {"skills_add": {"category": "Languages", "items": ["Python", "Go"]}},
        {"skills_add": {"category": "Infra", "items": ["Docker"]}},
        {"extra_add": {"label": "Open source", "value": "maintainer of wirefmt"}},
    ])
    assert out["skills"] == {"Languages": ["Python", "Go"], "Infra": ["Docker"]}
    assert out["extra"] == {"Note": "old", "Open source": "maintainer of wirefmt"}


def test_a_rejected_card_changes_nothing() -> None:
    """Skipping is the default safety valve; it must be a true no-op."""
    raw = {"headline": "Backend Engineer",
           "experience": [{"company": "C", "title": "T", "bullets": []}]}
    assert apply_patches(raw, []) == {**raw, "screening": {}}


def test_cards_do_not_re_propose_what_is_already_there() -> None:
    """Organizing twice from overlapping material is normal, not an error."""
    current = {"headline": "Backend Engineer", "languages": ["English"]}
    proposal = {"headline": "Backend Engineer",          # identical
                "languages": ["English", "Urdu"],        # one new
                "summary": "Two sentences."}             # new
    kinds = {c["kind"]: c for c in to_cards(proposal, current)}
    assert "headline" not in kinds, "an unchanged value is not a decision"
    assert kinds["summary"]["body"] == "Two sentences."
    assert kinds["list"]["body"] == ["Urdu"], "only the genuinely new item"


def test_flat_location_keys_land_in_identity_location() -> None:
    """The card said "CITY San Francisco"; it has to actually arrive.

    The extractor reports location flat, the record nests it. Writing it flat
    meant pydantic silently dropped it -- an accepted card that did nothing.
    """
    out = apply_patches({}, [{"identity": {
        "first_name": "Ayaan", "email": "a@b.com",
        "city": "San Francisco", "state": "CA", "country": "United States",
        "postal_code": "94107",
    }}])
    assert out["identity"]["first_name"] == "Ayaan"
    assert out["identity"]["location"] == {
        "city": "San Francisco", "state": "CA",
        "country": "United States", "postal_code": "94107",
    }


def test_flat_location_merges_into_an_existing_location() -> None:
    out = apply_patches(
        {"identity": {"location": {"city": "Oakland", "country": "United States"}}},
        [{"identity": {"city": "San Francisco", "state": "CA"}}])
    assert out["identity"]["location"] == {
        "city": "San Francisco", "state": "CA", "country": "United States"}


def test_work_arrangement_is_proposed_as_its_own_card() -> None:
    """"SF or remote" is a statement about arrangement; it must be reviewable."""
    cards = {c["kind"]: c for c in to_cards(
        {"work_arrangement": {"remote_ok": True, "onsite_ok": True}}, {})}
    assert cards["arrangement"]["patch"] == {"remote_ok": True, "onsite_ok": True}
    assert apply_patches({}, [cards["arrangement"]["patch"]])["remote_ok"] is True
