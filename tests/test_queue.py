"""The queue decides what gets applied to, so its two dangerous mistakes get a test.

Applying to a blacklisted job means a second application to an employer the
candidate already approached personally. Losing an approval means a run that
silently does nothing. Both are silent failures, so both are pinned here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from jobbot.discovery.sources import ATS
from jobbot.queue import JobQueue


@dataclass
class FakePost:
    native_id: str
    company: str = "acme"
    title: str = "Engineer Intern"
    url: str = "https://example.com/j"
    location: str = "Remote"
    ats: ATS = ATS.GREENHOUSE
    posted_at: datetime | None = None
    remote: bool = False
    department: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        return f"{self.ats.value}:{self.native_id}"


def test_a_decision_survives_rediscovery(tmp_path) -> None:
    """Discovery runs again every time; it must not clear what was decided."""
    q = JobQueue(tmp_path / "queue.json")
    q.add_posts([FakePost("1"), FakePost("2")])
    q.decide_many({"greenhouse:1": "blacklist", "greenhouse:2": "approved"})

    # same jobs come back from a later discovery, with an edited title
    again = JobQueue(tmp_path / "queue.json")
    again.add_posts([FakePost("1", title="Engineer II Intern"), FakePost("2"), FakePost("3")])

    assert again.blacklisted_ids() == {"greenhouse:1"}, \
        "a job the candidate is handling themselves must stay blacklisted"
    assert again.approved_ids() == {"greenhouse:2"}
    assert again.get("greenhouse:1").title == "Engineer II Intern", "the posting still refreshes"
    assert again.get("greenhouse:3").decision == "pending", "new jobs are not opted in"


def test_nothing_is_approved_by_default(tmp_path) -> None:
    """An empty tick list means apply to nothing, never apply to everything."""
    q = JobQueue(tmp_path / "queue.json")
    q.add_posts([FakePost(str(i)) for i in range(5)])
    assert q.approved_ids() == set()


def test_an_unknown_decision_is_refused(tmp_path) -> None:
    q = JobQueue(tmp_path / "queue.json")
    q.add_posts([FakePost("1")])
    assert not q.decide("greenhouse:1", "yolo")
    assert q.get("greenhouse:1").decision == "pending"


def test_a_corrupt_file_does_not_silently_start_empty(tmp_path) -> None:
    """Starting from empty would rewrite every decision away on the next save."""
    p = tmp_path / "queue.json"
    p.write_text("{not json", encoding="utf-8")
    try:
        JobQueue(p)
    except RuntimeError as exc:
        assert "unreadable" in str(exc)
    else:
        raise AssertionError("a corrupt queue must refuse to load, not reset")


def test_only_student_roles_survive_the_intern_filter(tmp_path) -> None:
    """"Internal", "International" and "Internship Program Manager" all contain
    the substring; none of them is an internship."""
    from jobbot.queue import is_internship

    for title in ("Software Engineer Intern", "Summer 2027 Analyst",
                  "Software Engineering Co-op", "Apprentice Engineer"):
        assert is_internship(title), title
    for title in ("Internal Tools Engineer", "International Sales Lead",
                  "Internship Program Manager", "Senior Software Engineer",
                  "Software Engineer"):
        assert not is_internship(title), title


def test_the_filter_never_drops_a_decision(tmp_path) -> None:
    """A blacklisted job stays blacklisted even if its title reads full-time."""
    q = JobQueue(tmp_path / "queue.json")
    q.add_posts([FakePost("1", title="Staff Engineer"), FakePost("2", title="SWE Intern")])
    q.decide("greenhouse:1", "blacklist")
    kept, dropped = q.keep_only_internships()
    assert (kept, dropped) == (2, 0)
    assert q.blacklisted_ids() == {"greenhouse:1"}


def test_stale_postings_sink_below_fresh_ones_at_equal_fit(tmp_path):
    """A 2021 posting at fit 0.94 must not outrank a last-week one at 0.90."""
    from datetime import date, timedelta

    from jobbot.queue import JobQueue, QueueEntry, is_stale

    today = date.today()
    q = JobQueue(tmp_path / "queue.json")
    q._entries = {
        "old": QueueEntry("old", company="a", fit=0.94,
                          posted=(today - timedelta(days=900)).isoformat()),
        "new": QueueEntry("new", company="b", fit=0.90,
                          posted=(today - timedelta(days=6)).isoformat()),
        "undated": QueueEntry("undated", company="c", fit=0.80, posted=""),
        "picked": QueueEntry("picked", company="d", fit=0.10, decision="approved",
                             posted=(today - timedelta(days=900)).isoformat()),
    }
    order = [e.job_id for e in q.all()]
    assert order == ["picked", "new", "undated", "old"]
    assert is_stale((today - timedelta(days=121)).isoformat(), today=today)
    assert not is_stale((today - timedelta(days=119)).isoformat(), today=today)
    assert not is_stale("", today=today)
    assert not is_stale("not-a-date", today=today)
