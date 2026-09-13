"""Guards for bugs that failed silently.

Each of these shipped as a wrong answer or a skipped posting rather than a
crash, which is why they need a test at all: nothing in a normal run surfaces
them. Every assertion here fails on the unfixed code.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from jobbot.ats.detect import ATS
from jobbot.discovery.sources import JobPost
from jobbot.llm.client import _is_quota_exhausted, _is_transient
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


def test_description_less_posting_is_not_filtered_out(profile: Profile) -> None:
    """Workday's search API returns no description; that must not filter.

    Unfixed, the skills component scored 0 against a bare title and every
    Workday posting landed at 0.4 -- under the 0.5 default threshold.
    """
    assert fit_score(profile, _post(description="")) >= 0.5


def test_description_still_drives_the_score(profile: Profile) -> None:
    """The no-description branch must not flatten real scoring."""
    matching = _post(ats=ATS.GREENHOUSE,
                     description="We use Python, Go, PostgreSQL, Docker, Redis.")
    unrelated = _post(ats=ATS.GREENHOUSE, title="Veterinary Technician",
                      description="Animal husbandry and clinic scheduling.")
    assert fit_score(profile, matching) > fit_score(profile, unrelated)
    assert fit_score(profile, unrelated) < 0.5


def test_plain_429_is_transient_not_quota_exhaustion() -> None:
    """An ordinary per-minute 429 must stay retryable.

    Unfixed, `rate_limit_error` counted as exhaustion, which flipped the run to
    the fallback backend permanently on the first burst of concurrency.
    """
    exc = Exception(
        "Error code: 429 - {'type': 'error', 'error': "
        "{'type': 'rate_limit_error', 'message': 'rate limit exceeded'}}"
    )
    assert not _is_quota_exhausted(exc)
    assert _is_transient(exc)


def test_real_exhaustion_still_detected() -> None:
    assert _is_quota_exhausted(Exception("You are out of extra usage credits"))
    assert _is_quota_exhausted(Exception("Quota exceeded for this window"))


@pytest.mark.parametrize("label", [
    "Bachelor's Degree",
    "I don't wish to answer",
    'He said "yes"',
    "Yes",
])
def test_selector_quoting_survives_apostrophes(label: str) -> None:
    """Option text goes into selectors; employer text contains apostrophes.

    Unfixed, `label:has-text('Bachelor's Degree')` was unparseable, all three
    radio fallbacks threw identically, and the field was left blank.
    """
    from jobbot.forms.fill import q      # imported here so the other guards
                                        # still collect on unfixed code
    quoted = q(label)
    assert quoted.startswith('"') and quoted.endswith('"')
    # The quoted form must round-trip to the original label exactly, or we
    # would be clicking an option that does not exist.
    import json
    assert json.loads(quoted) == label


def test_a_mid_stream_overload_is_retried_not_fatal() -> None:
    """The exact object the SDK raises for an `error` event during streaming.

    The HTTP response was a 200 -- the error arrived inside the stream -- so
    the SDK builds a generic APIStatusError with status_code=200. Trusting that
    status over the body classified an Anthropic overload as permanent: zero
    retries, and a real application marked failed seven seconds after the
    resume had rendered.
    """
    import anthropic
    import httpx

    from jobbot.llm.client import _is_quota_exhausted, _is_transient

    body = {"type": "error", "request_id": "req_x",
            "error": {"type": "overloaded_error", "message": "Overloaded"}}
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = anthropic.Anthropic(api_key="test")
    exc = client._make_status_error(f"{body}", body=body, response=httpx.Response(200, request=req))
    assert type(exc).__name__ == "APIStatusError" and exc.status_code == 200
    assert _is_transient(exc), "an overload must be retried"
    assert not _is_quota_exhausted(exc), "and must not flip the run to the fallback"

    # a genuine client error on the same path stays permanent
    bad = client._make_status_error(
        "{'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'bad'}}",
        body={}, response=httpx.Response(400, request=req))
    assert not _is_transient(bad)


def test_a_closed_dropdowns_placeholder_is_not_an_option() -> None:
    """Greenhouse's react-select reports exactly one option from the closed DOM:
    "Select...". Matching a confirmed answer against that list fails every time,
    so 8 of 11 blockers on a real form were "'Yes' matches none of
    ['Select...']" -- fields the fill layer opens and matches live anyway.
    """
    from jobbot.forms.model import FieldKind, FieldOption, FormField
    from jobbot.healer.answer import deterministic_answers, real_options
    from jobbot.forms.model import ParsedForm
    from jobbot.profile import Profile

    closed = FormField("q1", "Do you require visa sponsorship?", FieldKind.COMBOBOX,
                       required=True, options=[FieldOption("Select...")])
    assert real_options(closed) == []
    real = FormField("q2", "Veteran Status", FieldKind.SELECT,
                     options=[FieldOption("Select..."), FieldOption("Yes"), FieldOption("No")])
    assert real_options(real) == ["Yes", "No"], "a placeholder mixed into real options is dropped"

    prof = Profile.model_validate({
        "identity": {"first_name": "J", "last_name": "D", "email": "j@d.com"},
        "screening": {"requires_sponsorship_now": False,
                      "requires_sponsorship_future": False, "veteran_status": "No"},
    })
    answers, leftover = deterministic_answers(prof, ParsedForm(fields=[closed, real]))
    by = {a.field_id: a for a in answers}
    assert by["q1"].value == "No" and not by["q1"].needs_human, \
        "unknown options: pass the conventional word through for fill time"
    assert by["q2"].value == "No" and not by["q2"].needs_human
    assert leftover == []


def test_sponsorship_now_and_future_are_different_questions() -> None:
    """Both wordings are verbatim from a live Greenhouse form.

    A candidate on OPT answers now=No, future=Yes. The old first pattern
    matched `require.*visa.*sponsor` and routed the present-tense question to
    the future key -- a wrong answer to a legally significant question.
    """
    from jobbot.forms.model import FieldKind, FormField
    from jobbot.healer.answer import classify

    now = FormField("a", "Do you require visa sponsorship?", FieldKind.COMBOBOX)
    future = FormField("b", "Will you now or will you in the future require employment "
                            "visa sponsorship to work in the country in which the job "
                            "you're applying for is located?", FieldKind.COMBOBOX)
    classify(now); classify(future)
    assert now.profile_key == "requires_sponsorship_now"
    assert future.profile_key == "requires_sponsorship_future"
    assert now.legally_significant and future.legally_significant


def test_a_live_profile_holder_is_named_a_dead_one_is_cleared(tmp_path) -> None:
    """A run killed mid-flight leaves Chromium holding the persistent profile.

    Every later run then died inside Playwright with "Opening in existing
    browser session" -- no pid, no hint that the profile was the problem, and
    no way to recover but to find the process by hand.
    """
    import os

    from jobbot.browser.session import BrowserConfig, BrowserSession

    prof = tmp_path / "main"
    prof.mkdir()
    sess = BrowserSession(BrowserConfig(profile_dir=prof))

    assert sess._singleton_holder() is None, "no lock file at all is not a holder"

    # a lock naming this very process: genuinely held
    os.symlink(f"somehost-{os.getpid()}", prof / "SingletonLock")
    assert sess._singleton_holder() == os.getpid()

    # a lock naming a pid that cannot exist: stale, and clearable
    (prof / "SingletonLock").unlink()
    os.symlink("somehost-2147483647", prof / "SingletonLock")
    (prof / "SingletonCookie").symlink_to("123")
    assert sess._singleton_holder() is None
    assert sess._clear_stale_lock()
    assert not (prof / "SingletonLock").exists()
    assert not (prof / "SingletonCookie").exists()


def test_the_verifier_is_told_the_pages_own_validity_state() -> None:
    """A colour cannot distinguish "required" from "invalid".

    On Greenhouse a filled, valid, required field carries an accent that reads
    as an error outline in a screenshot. The verifier reported it as a blocker,
    the heal loop could not clear it (nothing was wrong), and the run could
    never reach `prepared`. The DOM reported aria-invalid="false" throughout.
    """
    from jobbot.forms.model import FieldKind, FormField, ParsedForm
    from jobbot.healer.checkpoints import _validity_block

    form = ParsedForm(fields=[
        FormField("a", "Are you open to relocation?", FieldKind.COMBOBOX,
                  required=True, selector="#a"),
        FormField("b", "Email", FieldKind.EMAIL, required=True, selector="#b"),
    ])
    all_valid = _validity_block(form, {
        "a": {"invalid": False, "message": "", "value_len": 2},
        "b": {"invalid": False, "message": "", "value_len": 9},
    })
    assert "All 2 controls report VALID" in all_valid
    assert "coloured border alone is not evidence" in all_valid

    one_bad = _validity_block(form, {
        "a": {"invalid": True, "message": "Please select an item in the list", "value_len": 0},
        "b": {"invalid": False, "message": "", "value_len": 9},
    })
    assert "reporting INVALID" in one_bad
    assert "Are you open to relocation?: Please select an item" in one_bad

    assert _validity_block(form, {}) == "", "no signal, no claim"


def test_a_richer_profile_does_not_score_lower_on_the_same_job() -> None:
    """The skill bar scaled with profile size: 69 skills needed 24 hits.

    A real 69-skill profile scored 0.35-0.40 on every one of 598 postings and
    the run attempted nothing. Same job, same matching skills, fewer listed
    skills -> higher score is the wrong way round.
    """
    from jobbot.ats.detect import ATS
    from jobbot.discovery.sources import JobPost
    from jobbot.orchestrator import fit_score
    from jobbot.profile import Profile

    jd = "We want Python, Go, PostgreSQL, Docker, Kubernetes, Redis, Kafka and Linux."
    post = JobPost(ats=ATS.GREENHOUSE, native_id="1", company="C",
                   title="Software Engineer", url="u", description=jd)
    core = ["Python", "Go", "PostgreSQL", "Docker", "Kubernetes", "Redis", "Kafka", "Linux"]
    lean = Profile.model_validate({"identity": {}, "target_titles": ["Software Engineer"],
                                   "skills": {"Core": core}})
    rich = Profile.model_validate({"identity": {}, "target_titles": ["Software Engineer"],
                                   "skills": {"Core": core, "Also": [f"skill{i}" for i in range(60)]}})
    assert fit_score(rich, post) == fit_score(lean, post), \
        "listing more skills must not lower the score for the same matching set"
    assert fit_score(rich, post) >= 0.9


def test_title_match_survives_word_order_and_a_level_suffix() -> None:
    """"Machine Learning Engineer Intern" describes "Machine Learning
    Infrastructure Engineer"; a substring test said it did not, and every title
    on a 598-posting board scored the no-match floor."""
    from jobbot.orchestrator import _title_match

    targets = ["Machine Learning Engineer Intern", "Software Engineering Intern"]
    assert _title_match(targets, "Machine Learning Infrastructure Engineer")[0] == 1.0
    assert _title_match(targets, "Senior Software Security Engineer")[1] == 1.0, \
        "'engineering' must meet 'engineer'"
    assert _title_match(targets, "Product Support Specialist") == (0.45, 0.0), \
        "no shared word: floor, and flagged as such"
    partial = _title_match(targets, "Research Engineer, Discovery")
    assert 0.45 < partial[0] < 1.0


def test_a_title_sharing_no_word_with_any_target_is_not_a_match() -> None:
    """A description mentioning Python and Linux is not enough on its own."""
    from jobbot.ats.detect import ATS
    from jobbot.discovery.sources import JobPost
    from jobbot.orchestrator import fit_score
    from jobbot.profile import Profile

    prof = Profile.model_validate({"identity": {},
        "target_titles": ["Machine Learning Engineer Intern"],
        "skills": {"x": ["Python", "Linux", "Git", "SQL", "Docker", "Bash", "Jira", "Slack"]}})
    jd = "Python Linux Git SQL Docker Bash Jira Slack -- help customers."
    support = JobPost(ats=ATS.GREENHOUSE, native_id="1", company="C",
                      title="Product Support Specialist", url="u", description=jd)
    ml = JobPost(ats=ATS.GREENHOUSE, native_id="2", company="C",
                 title="Machine Learning Infrastructure Engineer", url="u", description=jd)
    assert fit_score(prof, ml) >= 0.9
    assert fit_score(prof, support) < 0.5, "identical description, unrelated title -> below threshold"


def test_excluded_companies_are_never_queued() -> None:
    """The user had already applied to Palantir by hand; the run picked it as
    its top match anyway. There was no way to say "not this one"."""
    from jobbot.profile import Profile

    p = Profile.model_validate({"identity": {}, "exclude_companies": ["Palantir", "Example Corp"]})
    assert p.excludes("palantir") and p.excludes("Palantir Technologies")
    assert p.excludes("EXAMPLE CORP")
    assert not p.excludes("Nuro") and not p.excludes("")
    assert not Profile.model_validate({"identity": {}}).excludes("Palantir")


def test_a_state_code_matches_the_state_name() -> None:
    """A location dropdown says "Seattle, WA"; the answer says the state in full.

    Those scored below every threshold, so a required location field stayed
    empty through four heal rounds and the application could not be submitted.
    """
    from jobbot.forms.matching import match_option, normalize

    assert match_option("Seattle, Washington, United States",
                        ["Seattle, WA", "Costa Mesa, CA (HQ)"])[0] == "Seattle, WA"
    assert match_option("San Jose, California, United States",
                        ["San Jose, CA", "Austin, TX"])[0] == "San Jose, CA"

    # Only a trailing code expands: these are ordinary words mid-sentence.
    assert "indiana" not in normalize("will you work in office")
    assert "oregon" not in normalize("answer yes or no")
    assert match_option("Yes", ["Yes", "No"])[0] == "Yes"


def test_a_selector_survives_an_id_that_starts_with_a_digit() -> None:
    """CSS.escape belongs in an identifier, not inside a quoted attribute.

    Ashby ids are UUIDs, so about half begin with a digit. Escaping those into
    "#\\32 ba77b0f-..." produced a selector matching nothing, and two required
    fields -- LinkedIn and an essay question -- were silently never filled.
    """
    import re
    from pathlib import Path

    js = Path("jobbot/forms/extract.py").read_text(encoding="utf-8")
    assert 'return `[id="${attrEsc(el.id)}"]`' in js, \
        "the id selector must be an attribute selector, which needs no escaping"
    assert not re.search(r'label\[for="\$\{esc\}"\]', js), \
        "a CSS-escaped id inside a quoted attribute matches nothing"


def test_yes_maps_onto_a_sentence_without_asserting_a_fact() -> None:
    """Cloudflare offers three sentences where the answer we hold is "Yes".

    "Yes" matched none of them and a required field stayed empty. Mapping it is
    safe only in one direction: a willingness is true of someone open to
    relocating, whereas "I currently live in this job's location" is a claim
    about where they live.
    """
    from jobbot.forms.matching import match_yes_no_prose

    opts = ["I currently live in this job's location.",
            "I am willing to relocate to this job's location.",
            "I do not live and not willing to relocate to this job's location."]
    assert match_yes_no_prose("Yes", opts) == opts[1], "prefer the willingness"
    assert match_yes_no_prose("No", opts) == opts[2]

    # Leave literal yes/no lists to the ordinary matcher.
    assert match_yes_no_prose("Yes", ["Yes", "No"]) is None
    # Two factual affirmatives: declining beats putting words in their mouth.
    assert match_yes_no_prose(
        "Yes", ["I have a degree", "I have a licence", "I have neither"]) is None


def test_work_authorisation_picks_the_variant_the_profile_supports() -> None:
    """Forms qualify "Yes" with whether sponsorship will be needed.

    Answering the bare "Yes" let the matcher pick whichever variant it reached
    first, which then read as a mismatch at verification. The candidate's own
    confirmed sponsorship answers decide which is true, and both must come from
    the profile -- neither is a guess.
    """
    from jobbot.healer.answer import deterministic_answers
    from jobbot.forms.model import FieldKind, FieldOption, FormField, ParsedForm
    from jobbot.profile import Profile

    opts = ["Yes, and I will not need visa sponsorship",
            "Yes, but I will need visa sponsorship in the future", "No"]

    def answer_for(*, needs: bool) -> str:
        p = Profile.model_validate({
            "identity": {"first_name": "A", "last_name": "B", "email": "a@b.com"},
            "screening": {"work_authorization": True,
                          "requires_sponsorship_future": needs,
                          "requires_sponsorship_now": needs},
        })
        f = FormField(field_id="q", label="Are you authorized to work in the United States?",
                      kind=FieldKind.RADIO,
                      options=[FieldOption(label=o) for o in opts], required=True)
        got, _ = deterministic_answers(p, ParsedForm(fields=[f]))
        return str(got[0].value)

    assert answer_for(needs=True) == opts[1]
    assert answer_for(needs=False) == opts[0]


def test_an_ashby_posting_points_at_its_application_form() -> None:
    """Ashby serves the description at /<slug>/<id> and the form one level down.

    Where the board's applyUrl omitted the suffix, the parse found no fields at
    all and Exa and Deepgram were written off as unfillable -- while AfterQuery,
    Binance and Circleback, whose URLs carried it, went through.
    """
    from jobbot.discovery.sources import _ashby_apply_url

    assert _ashby_apply_url({"applyUrl": "https://jobs.ashbyhq.com/exa/abc"}) == \
        "https://jobs.ashbyhq.com/exa/abc/application"
    # Already correct, and the query string survives.
    assert _ashby_apply_url(
        {"applyUrl": "https://jobs.ashbyhq.com/cb/abc/application?embed=true"}) == \
        "https://jobs.ashbyhq.com/cb/abc/application?embed=true"
    assert _ashby_apply_url({"jobUrl": "https://jobs.ashbyhq.com/dg/abc?embed=true"}) == \
        "https://jobs.ashbyhq.com/dg/abc/application?embed=true"
    assert _ashby_apply_url({}) == ""


def test_every_module_imports_what_it_uses() -> None:
    """A missing import only surfaces when the branch runs.

    `asyncio.to_thread` was added to the healer's option-choosing path in a
    module that did not import asyncio. Nothing failed until a form needed that
    path, and then two applications died with "name 'asyncio' is not defined"
    after being filled -- work thrown away by a one-line omission that no test
    covered.
    """
    import ast
    from pathlib import Path

    stdlib = {"asyncio", "json", "re", "time", "os", "contextlib", "random",
              "shutil", "traceback", "unicodedata", "csv", "html", "base64", "io"}
    problems = []
    for path in sorted(Path("jobbot").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.asname or a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imported |= {a.asname or a.name for a in n.names}
        used = {n.value.id for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        for name in sorted(used & stdlib):
            if name not in imported:
                problems.append(f"{path}: uses {name}. without importing it")
    assert not problems, "\n".join(problems)


def test_no_does_not_tick_a_checkbox() -> None:
    """bool("No") is True, and that ticked every box in a choice group.

    On HP IQ's "How did you hear about HP IQ?" the model answered the options
    it did not mean with the string "No", and all seven were selected --
    ColorStack, hackthehood, A friend, TEDTalk and the rest. The verifier
    called it correctly: claiming a source the candidate did not come from is a
    false statement to an employer.
    """
    from jobbot.forms.fill import _as_bool

    for falsey in ("No", "no", "NO", "false", "0", "unchecked", "N/A", "", None, False):
        assert _as_bool(falsey) is False, falsey
    for truthy in ("Yes", "yes", "true", "1", "I agree", "confirmed", True):
        assert _as_bool(truthy) is True, truthy
    # Anything we cannot read is left alone rather than guessed either way.
    assert _as_bool("maybe") is None
    assert _as_bool("it depends") is None


def test_a_refused_submit_is_not_recorded_as_submitted() -> None:
    """Dedalus Labs answered a submit with why it refused, and we filed it as sent.

    "Your form needs corrections -- Missing entry for required field" is
    positive evidence the application did NOT go through. Recorded as
    SUBMITTED, already_applied then skips the job permanently, so a form that
    was never sent can never be retried. A blank response is a different case:
    that one stays ambiguous on purpose.
    """
    from jobbot.orchestrator import _looks_rejected

    assert _looks_rejected(
        "Your form needs corrections - Missing entry for required field: languages", [])
    assert _looks_rejected("", ["Please complete all required fields"])
    assert not _looks_rejected(
        "Thank you for applying! Your application has been received.", [])
    assert not _looks_rejected("", []), "no evidence stays ambiguous, not rejected"


def test_only_unsettled_failures_are_retried() -> None:
    """Retry the job until it is in -- but not when the answer will not change.

    A knockout, a posting with no form, and a question only the candidate can
    answer are settled: repeating them burns the run. A browser timeout or an
    unhealed field is not.
    """
    from jobbot.orchestrator import ApplicationResult, _worth_retrying
    from jobbot.tracker.csv_tracker import Status

    def r(status, reason="", flagged=None):
        return ApplicationResult("j", status, reason, flagged=flagged or [])

    assert not _worth_retrying(r(Status.CONFIRMED.value))
    assert not _worth_retrying(r(Status.SUBMITTED.value))
    assert not _worth_retrying(r(Status.KNOCKOUT_FAIL.value, "requires citizenship"))
    assert not _worth_retrying(r(Status.UNREACHABLE.value,
                                 "only third-party apply offered: Apply With Indeed"))
    assert not _worth_retrying(r(Status.FAILED.value, "no answerable fields found"))
    assert not _worth_retrying(r(Status.NEEDS_HUMAN.value,
                                 "legally significant answer missing from profile"))

    assert _worth_retrying(r(Status.NEEDS_HUMAN.value, "verification not clean"))
    assert not _worth_retrying(r(Status.NEEDS_HUMAN.value,
                                 "awaiting candidate: Agreement to Arbitrate"))
    assert _worth_retrying(r(Status.UNREACHABLE.value, "Timeout 8000ms exceeded"))
    assert _worth_retrying(r(Status.FAILED.value, "sign-in did not take"))


def test_a_workday_posting_is_read_in_english() -> None:
    """Blackstone's link arrives as /zh-CN/ and the whole form renders in Chinese.

    The fields came back as 名, 姓, 地址行 1, and the vision pass -- unable to
    recognise a form it could not read -- reported a sign-in gate that was no
    longer there, on every attempt.
    """
    from jobbot.discovery.sources import workday_en_url

    assert workday_en_url(
        "https://blackstone.wd1.myworkdayjobs.com/zh-CN/Campus/job/Miami/X_1") == \
        "https://blackstone.wd1.myworkdayjobs.com/en-US/Campus/job/Miami/X_1"
    # No locale segment, or already English: unchanged.
    assert workday_en_url(
        "https://allegion.wd5.myworkdayjobs.com/careers/job/Indy/X_1") == \
        "https://allegion.wd5.myworkdayjobs.com/careers/job/Indy/X_1"
    assert workday_en_url("https://boards.greenhouse.io/x/jobs/1") == \
        "https://boards.greenhouse.io/x/jobs/1"


def test_phone_adjacent_fields_do_not_get_the_phone_number() -> None:
    """Workday puts Phone Extension and Phone Device Type beside Phone Number.

    All three matched the phone pattern, so the candidate's number was entered
    as his extension and as his device type -- wrong data on the form, not a
    gap in it.
    """
    from jobbot.forms.model import FieldKind, FormField
    from jobbot.healer.answer import classify

    def key_for(label: str):
        f = FormField(field_id="x", label=label, kind=FieldKind.TEXT)
        classify(f)
        return f.profile_key

    assert key_for("Phone Number") == "identity.phone"
    assert key_for("Mobile Phone") == "identity.phone"
    assert key_for("Phone Extension") is None
    assert key_for("Phone Device Type") is None
    assert key_for("Country Phone Code") == "identity.phone_country"


def test_a_year_only_question_is_not_answered_with_a_full_date() -> None:
    """"Expected Graduation Year" was filled with 12/31/2027 for a May 2028
    graduation: the date formatter ran on a field that wants four digits, and
    the form both rejected it and stated the wrong year."""
    from jobbot.forms.fill import _YEAR_ONLY

    for label in ("Expected Graduation Year", "Year of graduation", "Grad year"):
        assert _YEAR_ONLY.search(label), label
    for label in ("Graduation date", "Expected graduation date (month/year)",
                  "Start date"):
        assert not _YEAR_ONLY.search(label), label


def test_a_graduation_field_is_answered_from_the_profile(profile: Profile) -> None:
    """Nothing derived graduation, so the model composed a bare "2028" and the
    date picker discarded it -- the field stayed empty and the application
    halted. The profile states the date, so the answer comes from there."""
    from jobbot.forms.model import FieldKind, FormField, ParsedForm
    from jobbot.healer.answer import deterministic_answers

    def answer(label: str, kind: FieldKind) -> object:
        field = FormField("f1", label, kind, required=True)
        found, _ = deterministic_answers(profile, ParsedForm(fields=[field]))
        return found[0].value if found else None

    end = next(e.end for e in profile.education if e.end)
    assert answer("Expected Graduation Year", FieldKind.DATE) == end.isoformat()
    # "Are you a recent graduate?" is a yes/no, not a date.
    assert answer("Are you a recent graduate?", FieldKind.RADIO) != end.isoformat()


def test_a_smartrecruiters_posting_points_at_its_application_form() -> None:
    """The posting URL renders the advert with no Apply button anywhere, which
    was recorded as "no answerable fields found" on two live AbbVie
    internships. The board's own redirect to the form is ?oga=true."""
    from jobbot.discovery.sources import smartrecruiters_apply_url as apply_url

    assert apply_url("https://jobs.smartrecruiters.com/AbbVie/3743990014860306") == \
        "https://jobs.smartrecruiters.com/AbbVie/3743990014860306?oga=true"
    # An existing query string is kept, and the suffix is never doubled.
    assert apply_url("https://jobs.smartrecruiters.com/AbbVie/374?src=x") == \
        "https://jobs.smartrecruiters.com/AbbVie/374?src=x&oga=true"
    assert apply_url("https://jobs.smartrecruiters.com/AbbVie/374?oga=true") == \
        "https://jobs.smartrecruiters.com/AbbVie/374?oga=true"
    # Another board's URL is left exactly as it is.
    assert apply_url("https://boards.greenhouse.io/x/jobs/1") == \
        "https://boards.greenhouse.io/x/jobs/1"


def test_a_yes_no_answer_is_not_written_as_a_python_boolean(monkeypatch) -> None:
    """Exa asks the sponsorship question as free text. str(True) put the word
    "True" in front of the employer, and sponsorship is legally significant so
    the healer may not rewrite it -- the application could not proceed."""
    import asyncio

    from jobbot.forms import fill
    from jobbot.forms.model import (AnswerSource, FieldKind, FormField,
                                    ProposedAnswer)

    written: list[str] = []

    async def fake_fill_text(page, field, value, sequential=False):
        written.append(value)
        return True

    monkeypatch.setattr(fill, "fill_text", fake_fill_text)
    field = FormField("q1", "Do you require Visa sponsorship?", FieldKind.TEXT,
                      selector="#q1", required=True)
    for value, expected in ((True, "Yes"), (False, "No"), ("H-1B", "H-1B")):
        written.clear()
        answer = ProposedAnswer("q1", value, AnswerSource.PROFILE, 1.0, "confirmed")
        assert asyncio.run(fill.apply_answer(object(), field, answer))
        assert written == [expected], f"{value!r} was written as {written!r}"


def test_a_compound_sponsorship_question_gets_more_than_yes(profile: Profile) -> None:
    """Exa asks "Do you require Visa sponsorship? If so, which one? And when
    does your Visa expire?" as free text. "Yes" answers a third of it, and the
    verifier blocked it as uninformative on a field the healer may not touch.

    The candidate's own words go in verbatim -- nothing about immigration
    status is paraphrased -- and the unknown expiry is stated as unknown
    rather than invented or silently skipped.
    """
    import re

    from jobbot.healer.answer import _sponsorship_prose

    compound = ("Do you require Visa sponsorship to work in your selected location? "
                "If so, which one? And when does your Visa expire?")
    status = str(profile.answer("visa_status").value)
    said = _sponsorship_prose(profile, True, compound)
    assert said.startswith("Yes")
    assert status in said, "the candidate's own words, not a paraphrase"
    assert "expiry" in said, "the expiry part is answered, not dropped"
    assert not re.search(r"\b20\d\d\b", said), "an expiry date is never invented"

    assert _sponsorship_prose(profile, False, compound).startswith("No")


def test_a_rate_limit_is_not_retried() -> None:
    """Oracle answered a third attempt with "Too Many Attempts. Try Again
    Later." and locked out the tenant. Retrying a rate limit is what causes
    it, so it counts as settled."""
    from jobbot.orchestrator import ApplicationResult, _worth_retrying
    from jobbot.tracker.csv_tracker import Status

    limited = ApplicationResult(
        "oracle:1", Status.UNREACHABLE.value,
        "still on the email step: Too Many Attempts. Try Again Later.")
    assert not _worth_retrying(limited)
    # An ordinary timeout is still worth another go.
    assert _worth_retrying(ApplicationResult(
        "oracle:2", Status.UNREACHABLE.value, "navigation timeout"))


def test_both_ledgers_round_trip_through_the_file_lock(tmp_path) -> None:
    """The trackers lock a sidecar file around every write.

    The lock helper was swapped for a cross-platform one and the import was
    added to one tracker but not the other; nothing exercised the on-disk
    path, so the first real run died with NameError inside upsert().
    """
    from jobbot.tracker.answers_csv import AnswerLog
    from jobbot.tracker.csv_tracker import Application, Status, Tracker

    t = Tracker(tmp_path / "applications.csv")
    t.upsert(Application(job_id="gh:1", company="Acme", title="SWE",
                         ats="greenhouse", status=Status.DISCOVERED.value))
    t.upsert(Application(job_id="gh:1", company="Acme", title="SWE",
                         ats="greenhouse", status=Status.FILTERED_OUT.value))
    rows = t.all()
    assert len(rows) == 1 and rows[0].status == Status.FILTERED_OUT.value

    a = AnswerLog(tmp_path / "answers.csv")
    n = a.record(job_id="gh:1", company="Acme", title="SWE", ats="greenhouse",
                 job_url="https://x", answers=[{"label": "First Name", "value": "Jane"}])
    assert n == 1 and a.for_job("gh:1")[0]["answer"] == "Jane"
    assert (tmp_path / "applications.lock").exists()


def test_the_context_never_loses_its_last_window(tmp_path, monkeypatch) -> None:
    """Chromium on Windows/Linux exits when its last window closes.

    Closing the launch-time blank page, or the last leased tab between two
    applications, took the whole persistent context down with
    "BrowserContext.new_page: Target page, context or browser has been closed".
    A blank keeper page must therefore survive start(), every tab lease, and
    orphan reaping.
    """
    import asyncio

    from jobbot.browser import session as mod

    class FakePage:
        def __init__(self, ctx, url="about:blank"):
            self.ctx, self.url, self.closed = ctx, url, False
        def is_closed(self):
            return self.closed
        async def close(self):
            self.closed = True
            self.ctx.pages.remove(self)

    class FakeCtx:
        def __init__(self):
            self.pages = [FakePage(self)]
        def set_default_navigation_timeout(self, ms): pass
        def set_default_timeout(self, ms): pass
        async def new_page(self):
            if not self.pages:
                raise RuntimeError("Target page, context or browser has been closed")
            p = FakePage(self); self.pages.append(p); return p
        async def close(self):
            self.pages.clear()

    fake = FakeCtx()
    launch_blank = fake.pages[0]

    async def fake_launch(profile_dir, **kwargs):
        return fake

    monkeypatch.setattr(mod, "launch_persistent_context_async", fake_launch)
    sess = mod.BrowserSession(mod.BrowserConfig(profile_dir=tmp_path / "p"))

    async def scenario():
        await sess.start()
        assert launch_blank in fake.pages, "the launch blank page must be kept"
        assert sess.total_pages == 0, "the keeper is not a leased tab"

        for _ in range(3):                      # three applications in a row
            async with sess.tab("app") as page:
                page.url = "https://example.com/apply"
                assert sess.live_tabs == 1
            assert fake.pages, "closing the leased tab must not close the last window"

        await sess.reap_orphans()
        assert fake.pages and not fake.pages[0].is_closed(), "reaping must spare the keeper"

        async with sess.tab("x"):
            await launch_blank.close()          # something ate the keeper
        assert fake.pages, "a lost keeper is recreated before the tab closes"

    asyncio.run(scenario())


def test_an_application_is_retried_in_its_own_tab_until_it_settles(tmp_path, monkeypatch) -> None:
    """A crash mid-form used to close the tab and file the job as failed.

    Now the same tab is reused: two crashes, one unhealed result, then a
    dry-run success must come back as the success, with the tab never
    released in between. A settled result (knockout) must not be retried,
    and a tab that is actually gone must be reported as such so the outer
    loop can lease a new one.
    """
    import asyncio

    from jobbot import orchestrator as mod
    from jobbot.orchestrator import ApplicationResult, Orchestrator, RunConfig
    from jobbot.tracker.csv_tracker import Status

    monkeypatch.setattr(mod, "_retry_delay", lambda attempt: 0.0)

    class Page:
        closed = False
        def is_closed(self): return self.closed

    class Post:
        job_id, company, url, title = "gh:1", "Acme", "https://x/apply", "SWE"

    class Tracker:
        def __init__(self): self.updates = []
        def update(self, jid, **kw): self.updates.append(kw)

    orch = Orchestrator.__new__(Orchestrator)
    orch.cfg = RunConfig(data_dir=tmp_path)
    orch.tracker = Tracker()
    audit = tmp_path / "a"; audit.mkdir()

    script = [RuntimeError("gemini 503: high demand, please try again later"),
              RuntimeError("Timeout 60000ms exceeded"),
              ApplicationResult("gh:1", Status.FAILED.value, "2 required fields unhealed"),
              ApplicationResult("gh:1", "dry_run", "verified but not submitted")]
    calls = []

    async def fake_apply(page, post, audit_, shots):
        calls.append(page)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    orch._apply_in_tab = fake_apply
    page = Page()
    r = asyncio.run(orch._apply_with_retries(page, Post(), audit, audit / "s"))
    assert r.status == "dry_run"
    assert len(calls) == 4 and all(c is page for c in calls), "same tab every time"
    assert (audit / "error.txt").read_text(encoding="utf-8").count("--- attempt") == 2
    assert any(u.get("status") == Status.FILLING.value and "retry 2/25" in u["error"]
               for u in orch.tracker.updates)

    # settled: a knockout is final, no second try
    script[:] = [ApplicationResult("gh:1", Status.KNOCKOUT_FAIL.value, "sponsorship")]
    calls.clear()
    r = asyncio.run(orch._apply_with_retries(page, Post(), audit, audit / "s"))
    assert r.status == Status.KNOCKOUT_FAIL.value and len(calls) == 1

    # the budget is honoured when --attempts is given
    orch.cfg = RunConfig(data_dir=tmp_path, attempts_per_job=2)
    script[:] = [RuntimeError("a"), RuntimeError("b"), RuntimeError("c")]
    calls.clear()
    r = asyncio.run(orch._apply_with_retries(page, Post(), audit, audit / "s"))
    assert r.status == Status.FAILED.value and len(calls) == 2

    # a dead tab is reported, not retried in place
    dead = Page(); dead.closed = True
    script[:] = [RuntimeError("Target page, context or browser has been closed")]
    r = asyncio.run(orch._apply_with_retries(dead, Post(), audit, audit / "s"))
    assert r.reason.startswith("tab died")


def test_healer_reapplies_profile_values_and_snaps_declines(monkeypatch, tmp_path) -> None:
    """Two blockers seen on a live Greenhouse form, both previously unhealable.

    "Agreement to Arbitrate" is legally significant, so the healer refused to
    touch it -- even though the profile held a confirmed "Yes" and the box had
    simply not taken the first click. And for "Gender" the verifier suggested
    "I do not wish to answer" where the form's option is "Decline To Self
    Identify"; the filler resolves that, the healer did not. Both must heal
    now, the first from the profile and never from the model.
    """
    import asyncio

    from jobbot.forms import fill as fill_mod
    from jobbot.forms.model import (AnswerSource, FieldKind, FieldOption, FormField,
                                    ParsedForm, Verification, VerificationIssue)
    from jobbot.healer import checkpoints as ck
    from jobbot.profile import Profile

    arb = FormField("arb", "Agreement to Arbitrate", FieldKind.CONSENT, required=True)
    gender = FormField("g", "Gender", FieldKind.SELECT, options=[
        FieldOption("Male"), FieldOption("Female"), FieldOption("Decline To Self Identify")])
    form = ParsedForm(fields=[arb, gender])
    prof = Profile.model_validate({
        "identity": {"first_name": "J", "last_name": "D", "email": "j@d.com"},
        "screening": {"arbitration_agreement": "Yes"},
    })

    rounds = []

    async def fake_verify(page, llm, profile, form_, answers, shots, *, round_no):
        rounds.append(round_no)
        if round_no == 1:
            return Verification(issues=[
                VerificationIssue("arb", "Agreement to Arbitrate", "unchecked", "blocker", None),
                VerificationIssue("g", "Gender", "empty", "blocker", "I do not wish to answer"),
            ]), None
        return Verification(ready_to_submit=True), None

    applied = []

    async def fake_apply(page, f, patch, resume_path=None):
        applied.append((f.field_id, patch))
        return True

    monkeypatch.setattr(ck, "checkpoint_verify", fake_verify)
    monkeypatch.setattr(fill_mod, "apply_answer", fake_apply)

    answers: list = []
    v, n = asyncio.run(ck.heal(None, None, prof, form, answers, tmp_path, max_rounds=3))
    assert v.ready_to_submit and n == 2 and rounds == [1, 2]

    by = {fid: patch for fid, patch in applied}
    assert by["arb"].source is AnswerSource.PROFILE, "the candidate's own answer, not the model's"
    assert by["arb"].value not in (None, "", False)
    assert by["g"].value == "Decline To Self Identify"
    assert {a.field_id for a in answers} == {"arb", "g"}, "healed values reach the ledger"


def test_a_confirmed_yes_ticks_a_lone_consent_checkbox() -> None:
    """Greenhouse's arbitration agreement is one checkbox whose only option is
    the sentence "Please read the arbitration agreement below". The profile's
    confirmed "Yes" matched none of that text, so a live run filed it as
    "needs human" -- twice per pass -- and could never verify clean.
    """
    from jobbot.forms.model import FieldKind, FieldOption, FormField, ParsedForm
    from jobbot.healer.answer import deterministic_answers
    from jobbot.profile import Profile

    prof = Profile.model_validate({
        "identity": {"first_name": "J", "last_name": "D", "email": "j@d.com"},
        "screening": {"arbitration_agreement": "Yes", "policy_acknowledgement": False},
    })
    arb = FormField("a", "Please read the arbitration agreement below", FieldKind.CHECKBOX,
                    required=True, options=[FieldOption("Please read the arbitration agreement below")])
    arb2 = FormField("b", "Agreement to Arbitrate", FieldKind.CONSENT, required=True)
    pol = FormField("c", "I acknowledge the AI policy", FieldKind.CHECKBOX,
                    options=[FieldOption("I acknowledge the AI policy")])
    # the live Greenhouse shape: a combobox whose single option is the sentence
    sentence = "I have read and agree to the arbitration agreement"
    combo = FormField("d", "Agreement to Arbitrate", FieldKind.COMBOBOX, required=True,
                      options=[FieldOption(sentence)])
    answers, leftover = deterministic_answers(prof, ParsedForm(fields=[arb, arb2, pol, combo]))
    by = {a.field_id: a for a in answers}
    assert by["a"].value is True and by["a"].submittable
    assert by["b"].value is True and by["b"].submittable
    assert by["c"].value is False, "a confirmed No leaves the box alone"
    assert by["d"].value == sentence and by["d"].submittable, "a Yes picks the only option"
    assert leftover == []


def test_an_essay_mentioning_your_stack_is_not_a_placeholder() -> None:
    """The healer rejected a real 1,250-character "Why us?" answer because
    the placeholder regex matched "your " mid-sentence. Descriptions of a
    value open with a possessive or an adjective; real prose merely contains
    them.
    """
    from jobbot.healer.checkpoints import _looks_like_a_description as desc

    assert not desc("Anthropic is building infrastructure for research that matters. "
                    "I am proficient in Python, Go and PostgreSQL, which seem to be "
                    "core to your stack. The feedback loop you describe is compelling.")
    assert not desc("I rebuilt their ingestion path and cut p99 latency to 95ms.")
    assert desc("Candidate's real phone number")
    assert desc("your current address")
    assert desc("The actual value should be entered here")
    assert desc("[insert company name]")
    assert desc("TBD")
    assert desc("")


def test_the_dom_overrules_vision_on_a_scrolled_textarea() -> None:
    """A 1,250-character essay shows four lines in its box. Vision called it
    "visibly truncated" and blocked; three heal rounds re-typed the same full
    text and got the same verdict. The DOM knows the whole value.
    """
    import asyncio

    from jobbot.forms.model import (AnswerSource, FieldKind, FormField, ParsedForm,
                                    ProposedAnswer, VerificationIssue)
    from jobbot.healer.checkpoints import _dom_truth

    essay = "Anthropic is building infrastructure for research that matters. " * 20
    why = FormField("q1", "Why Anthropic?", FieldKind.TEXTAREA, required=True, selector="#q1")
    phone = FormField("q2", "Phone", FieldKind.PHONE, required=True, selector="#q2")
    form = ParsedForm(fields=[why, phone])
    answers = [ProposedAnswer("q1", essay, AnswerSource.COMPOSED, 1.0, "x"),
               ProposedAnswer("q2", "555", AnswerSource.PROFILE, 1.0, "x")]

    class Loc:
        def __init__(self, v): self.v = v; self.first = self
        async def input_value(self, timeout=0): return self.v

    class Page:
        def locator(self, sel): return Loc(essay if sel == "#q1" else "55")

    issues = [VerificationIssue("q1", "Why Anthropic?", "The answer is visibly truncated.", "blocker", essay),
              VerificationIssue("q2", "Phone", "visibly truncated", "blocker", "555"),
              VerificationIssue("q1", "Why Anthropic?", "empty", "blocker", None)]
    out, confirmed = asyncio.run(_dom_truth(Page(), form, answers, issues))
    assert out[0].severity == "warning" and "DOM holds" in out[0].problem
    assert out[1].severity == "blocker", "a phone box is not a scrolled essay"
    assert out[2].severity == "blocker", "only truncation claims are overruled"
    assert confirmed == {"why anthropic?"}


def test_voluntary_self_id_is_declined_not_invented(monkeypatch, tmp_path) -> None:
    """Gender and race are optional EEO fields the verifier lists in
    unfilled_required (no field_id, so the blocker loop never sees them). The
    profile holds no gender or race and must not. Declining is the honest
    non-answer; veteran/disability status, being legally significant, are left
    to the profile.
    """
    import asyncio

    from jobbot.forms import fill as fill_mod
    from jobbot.forms.model import (AnswerSource, FieldKind, FieldOption, FormField,
                                    ParsedForm, Verification)
    from jobbot.healer import checkpoints as ck
    from jobbot.profile import Profile

    gender = FormField("g", "Gender", FieldKind.SELECT, options=[
        FieldOption("Male"), FieldOption("Female"), FieldOption("Decline To Self Identify")])
    race = FormField("r", "Please identify your race", FieldKind.SELECT, options=[
        FieldOption("Asian"), FieldOption("White"), FieldOption("Decline To Self Identify")])
    vet = FormField("v", "Veteran Status", FieldKind.SELECT, options=[
        FieldOption("I am a protected veteran"),
        FieldOption("I am not a protected veteran"), FieldOption("Decline To Self Identify")])
    form = ParsedForm(fields=[gender, race, vet])
    prof = Profile.model_validate({
        "identity": {"first_name": "J", "last_name": "D", "email": "j@d.com"},
        "screening": {"veteran_status": "I am not a protected veteran"}})

    async def fake_verify(page, llm, profile, form_, answers, shots, *, round_no):
        if round_no == 1:
            return Verification(ready_to_submit=False,
                                unfilled_required=["Gender", "Please identify your race",
                                                   "Veteran Status"]), None
        return Verification(ready_to_submit=True), None

    applied = []

    async def fake_apply(page, f, patch, resume_path=None):
        applied.append((f.field_id, patch.value)); return True

    monkeypatch.setattr(ck, "checkpoint_verify", fake_verify)
    monkeypatch.setattr(fill_mod, "apply_answer", fake_apply)

    answers: list = []
    v, n = asyncio.run(ck.heal(None, None, prof, form, answers, tmp_path, max_rounds=3))
    by = dict(applied)
    assert by["g"] == "Decline To Self Identify"
    assert by["r"] == "Decline To Self Identify"
    assert "v" not in by, "veteran status is legally significant; the profile owns it"
    assert v.ready_to_submit
