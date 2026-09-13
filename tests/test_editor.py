"""The editor's one load-bearing distinction: `false` is not `unset`.

"No" is an answer. "Not set" is the absence of one, and it has to stay absent
through the whole round trip, because an absent key is what makes the run stop
and ask instead of submitting. If these two ever collapse into each other the
failure is silent and lands on a real application, so they get a test.
"""

from __future__ import annotations

import yaml

from jobbot.editor import from_form, save, to_form, validate
from jobbot.profile import Profile

BASE = {
    "identity": {"first_name": "A", "last_name": "B", "email": "a@b.com",
                 "location": {"city": "SF", "state": "CA"}},
    "experience": [], "education": [], "projects": [], "skills": [], "extra": [],
    "compensation": {}, "awards": [], "publications": [],
    "certifications": [], "languages": [],
}


def test_false_is_kept_and_unset_is_dropped() -> None:
    payload = {**BASE, "screening": {
        "criminal_history": False,        # answered: no
        "work_authorization": True,       # answered: yes
        "background_check_consent": None,  # not answered
        "citizenship": "",                # not answered
    }}
    scr = from_form(payload)["screening"]
    assert scr["criminal_history"] is False, "an explicit No must be preserved"
    assert scr["work_authorization"] is True
    assert "background_check_consent" not in scr, "unset must not be written"
    assert "citizenship" not in scr, "an empty string is not an answer"


def test_unset_key_blocks_the_run_and_false_does_not() -> None:
    """The whole point: absence halts, `false` proceeds."""
    answered = Profile.model_validate(from_form({**BASE, "screening": {
        k: False for k in
        ("work_authorization", "requires_sponsorship_now",
         "requires_sponsorship_future", "criminal_history",
         "background_check_consent", "veteran_status", "disability_status",
         "age_over_18", "education_degree", "previously_employed_here")}}))
    assert answered.missing_legally_significant() == [], \
        "every core question answered false is still fully answered"

    partial = Profile.model_validate(from_form({**BASE, "screening": {
        "criminal_history": False, "background_check_consent": None}}))
    assert "background_check_consent" in partial.missing_legally_significant()
    assert "criminal_history" not in partial.missing_legally_significant()


def test_round_trip_through_disk_preserves_false(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    res = save(path, from_form({**BASE, "screening": {"criminal_history": False}}))
    assert res["ok"], res
    back = to_form(yaml.safe_load(path.read_text()))
    assert back["screening"]["criminal_history"] is False, \
        "a No must survive save/load, not come back as unset"


def test_invalid_payload_leaves_the_file_untouched(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    save(path, from_form({**BASE, "screening": {}}))
    before = path.read_bytes()

    bad = {**BASE, "identity": {**BASE["identity"], "email": "not-an-email"}}
    res = save(path, from_form(bad))
    assert not res["ok"] and res["errors"]
    assert path.read_bytes() == before, "a rejected submit must not write"


def test_overwrite_keeps_a_backup(tmp_path) -> None:
    path = tmp_path / "profile.yaml"
    save(path, from_form(BASE))
    res = save(path, from_form({**BASE, "headline": "changed"}))
    assert res["backup"], "the previous answer set must be recoverable"
    old = yaml.safe_load(open(res["backup"]).read())
    assert old["headline"] == ""


def test_scalar_and_dict_screening_both_load(tmp_path) -> None:
    """On-disk screening may be a bare scalar or a {value, provenance} dict."""
    raw = yaml.safe_load("""
identity: {first_name: A, last_name: B, email: a@b.com}
screening:
  criminal_history: false
  work_authorization: {value: true, provenance: confirmed}
""")
    flat = to_form(raw)["screening"]
    assert flat["criminal_history"] is False
    assert flat["work_authorization"] is True
    prof, errs = validate(raw)
    assert prof is not None and not errs
    assert prof.can_answer("work_authorization")


def test_a_project_with_only_a_repo_still_gets_a_link() -> None:
    """The resume renders `url`; repo-only projects printed with no link."""
    out = from_form({**BASE, "projects": [
        {"name": "wirefmt", "repo": "github.com/me/wirefmt"},
    ]})
    assert out["projects"][0]["url"] == "https://github.com/me/wirefmt"


def test_bare_domains_get_a_scheme() -> None:
    """People dictate "github.com/me" -- a form field needs the scheme."""
    out = from_form({**BASE, "identity": {
        **BASE["identity"], "github": "github.com/me",
        "linkedin": "https://linkedin.com/in/me",
    }})
    assert out["identity"]["github"] == "https://github.com/me"
    assert out["identity"]["linkedin"] == "https://linkedin.com/in/me"


def test_a_partial_draft_saves_and_says_what_is_missing(tmp_path) -> None:
    """Saving used to be all-or-nothing.

    One missing email, or one role whose start date the source never stated,
    rejected the entire submission -- so 14 accepted proposals were thrown away
    over a field the extractor was explicitly told it could omit. A draft now
    saves, and reports its own gaps.
    """
    path = tmp_path / "profile.yaml"
    res = save(path, from_form({
        **BASE,
        "identity": {"first_name": "Ayaan", "last_name": "", "email": ""},
        "experience": [{"company": "Example Corp", "title": "Infra Engineer",
                        "bullets": ["Cut p99 840ms -> 95ms"]}],
    }))
    assert res["ok"], res
    assert res["missing_identity"] == ["identity.last_name", "identity.email"]
    assert res["undated_roles"] == ["Infra Engineer at Example Corp"]
    assert path.exists(), "the work must be on disk, not discarded"


def test_an_unset_email_is_none_not_empty_string() -> None:
    """"" is a str, and EmailStr rejects it -- which broke the optional field."""
    assert from_form({**BASE, "identity": {"first_name": "A"}})["identity"]["email"] is None


def test_a_malformed_email_is_still_rejected(tmp_path) -> None:
    """Optional is not the same as unvalidated."""
    path = tmp_path / "profile.yaml"
    res = save(path, from_form({**BASE, "identity": {
        **BASE["identity"], "email": "not-an-email"}}))
    assert not res["ok"]
    assert not path.exists()


def test_an_undated_role_does_not_break_the_date_maths() -> None:
    p = Profile.model_validate(from_form({**BASE, "experience": [
        {"company": "A", "title": "T"},                      # no dates at all
        {"company": "B", "title": "U", "start": "2023-01-01", "end": "2024-01-01"},
    ]}))
    assert p.total_years_experience == 1.0, "the undated role is skipped, not fatal"
    assert p.employment_gaps() == []


def test_the_run_refuses_what_the_save_allowed() -> None:
    """The gate moved to run time; it must actually be there."""
    p = Profile.model_validate(from_form({
        **BASE, "identity": {"first_name": "", "last_name": "", "email": ""}}))
    assert p.missing_identity() == [
        "identity.first_name", "identity.last_name", "identity.email"]


def test_a_placeholder_title_is_treated_as_absent() -> None:
    """A model filling a required field it cannot source writes "<UNKNOWN>".

    That string would be typeset onto a resume and typed into an employer's
    form, so it counts as missing -- but the role itself must survive, because
    requiring a title silently dropped real jobs.
    """
    out = from_form({**BASE, "experience": [
        {"company": "Example Corp", "title": "<UNKNOWN>", "bullets": ["x"]},
        {"company": "Tiny Startup", "title": "N/A"},
    ]})
    assert [e["company"] for e in out["experience"]] == ["Example Corp", "Tiny Startup"]
    assert all(e["title"] == "" for e in out["experience"])


def test_save_reports_untitled_roles(tmp_path) -> None:
    res = save(tmp_path / "p.yaml", from_form({**BASE, "experience": [
        {"company": "Example Corp", "title": "unknown"}]}))
    assert res["ok"] and res["untitled_roles"] == ["Example Corp"]


def test_an_absent_boolean_keeps_the_model_default() -> None:
    """`bool(data.get(k))` made "not mentioned" mean "no".

    An intake payload carries no remote_ok/onsite_ok/hybrid_ok, so every
    profile built from a dump claimed the candidate was open to no arrangement
    at all. On a live form that produced a contradiction the verifier caught:
    answered "Yes" to working in person, profile said open to nothing.
    """
    from jobbot.profile import Profile

    silent = Profile.model_validate(from_form({**BASE}))
    assert (silent.remote_ok, silent.onsite_ok, silent.hybrid_ok) == (True, True, True)
    assert silent.willing_to_relocate is False, "the model's own default, not a coercion"

    stated = Profile.model_validate(from_form({
        **BASE, "remote_ok": True, "onsite_ok": False,
        "hybrid_ok": True, "willing_to_relocate": True}))
    assert (stated.remote_ok, stated.onsite_ok) == (True, False)
    assert stated.willing_to_relocate is True, "an explicit choice still wins"


def test_a_gpa_written_the_way_people_write_it() -> None:
    """"3.8/4.0" is how a GPA appears on most resumes.

    float("3.8/4.0") raised ValueError out of the save, and one exception fails
    the whole request -- so a single slash discarded a submission carrying ten
    roles and eight projects.
    """
    from jobbot.editor import first_number

    assert first_number("3.8/4.0") == 3.8
    assert first_number("3.8 out of 4.0") == 3.8
    assert first_number("GPA 3.8") == 3.8
    assert first_number("3,8") == 3.8, "a decimal comma is a decimal point, not 3.0"
    assert first_number("A-") is None, "unparseable means absent, never a guess"
    assert first_number("") is None and first_number(None) is None

    out = from_form({**BASE,
                     "education": [{"school": "X", "gpa": "3.8/4.0"}],
                     "notice_period_weeks": "2 weeks"})
    assert out["education"][0]["gpa"] == 3.8
    assert out["notice_period_weeks"] == 2


def test_no_unparseable_field_can_abort_a_save(tmp_path) -> None:
    """Every numeric field takes free text without raising."""
    res = save(tmp_path / "p.yaml", from_form({
        **BASE,
        "education": [{"school": "X", "gpa": "first class honours"}],
        "notice_period_weeks": "immediately",
        "min_requirement_match": "half",
        "compensation": {"target_base": "competitive", "minimum_base": "$140,000"},
    }))
    assert res["ok"], res
    prof = yaml.safe_load((tmp_path / "p.yaml").read_text())
    assert prof["education"][0]["gpa"] is None
    assert prof["notice_period_weeks"] is None
    assert prof["min_requirement_match"] == 0.5
    assert prof["compensation"]["minimum_base"] == 140000


def test_salary_shorthand() -> None:
    """"165k" is how a target base gets typed, and it parsed to nothing."""
    def comp(t, m):
        return from_form({**BASE, "compensation": {"target_base": t, "minimum_base": m}})["compensation"]

    assert comp("165k", "140k") == {"target_base": 165000, "minimum_base": 140000, "currency": "USD"}
    assert comp("$165,000", "140000")["target_base"] == 165000
    assert comp("1.2m", "competitive") == {"target_base": 1200000, "minimum_base": None, "currency": "USD"}
