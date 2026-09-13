"""The shortlist: which discovered jobs the candidate has actually approved.

Discovery finds hundreds of postings and the fit filter ranks them, but neither
is a decision. This holds the decision -- one per job, made by a person -- and
the run loop applies to nothing that is not marked `approved`.

Four states, and the distinction between the last two matters:

  pending    discovered, not looked at yet
  approved   apply to this one
  blacklist  never apply -- the candidate is handling it personally
  applied    already sent, recorded so the list stops offering it

`blacklist` is not "rejected". A job the candidate applied to themselves must
never be applied to again by this software, and that has to survive a fresh
discovery run, which is why it is stored here rather than inferred from the
tracker.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import structlog

log = structlog.get_logger(__name__)


STALE_DAYS = 120


def is_stale(posted: str, *, today: date | None = None) -> bool:
    """True when the source's posted date is older than STALE_DAYS."""
    if not posted:
        return False
    try:
        d = date.fromisoformat(posted[:10])
    except ValueError:
        return False
    return ((today or date.today()) - d).days > STALE_DAYS


def _rank(x: QueueEntry) -> tuple:
    # posted is ISO YYYY-MM-DD, so string order is date order; negate via
    # reversal-free trick: sort ascending on the stale flag, descending on fit,
    # and descending on date by using a tuple of the inverted date parts.
    posted = x.posted[:10] if x.posted else "0000-00-00"
    newest_first = tuple(-int(part) for part in posted.split("-") if part.isdigit())
    return (x.decision != "approved", is_stale(x.posted), -x.fit, newest_first, x.company)


DECISIONS = ("pending", "approved", "blacklist", "applied")


@dataclass
class QueueEntry:
    job_id: str
    company: str = ""
    title: str = ""
    url: str = ""
    location: str = ""
    ats: str = ""
    decision: str = "pending"
    fit: float = 0.0
    discovered_at: str = ""
    decided_at: str = ""
    note: str = ""
    # Everything below is for reading the row and deciding, not for the run.
    posted: str = ""          # YYYY-MM-DD, as the source reported it
    term: str = ""            # "Summer 2027", "Fall 2026", ...
    sponsorship: str = ""     # what the source says about visa sponsorship
    remote: bool = False
    department: str = ""
    source: str = ""          # which list or board it came from

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Anything that is not a student position. "Internal", "International" and
# "Internship Program Manager" all contain the substring, so the match is on
# whole words, and the manager/full-time titles are excluded outright.
_INTERN = re.compile(
    r"\b(intern|interns|internship|intern's|co-?op|apprentice|apprenticeship)\b"
    r"|\bsummer\s+20\d\d\b|\bindustrial\s+placement\b", re.I)
_NOT_INTERN = re.compile(
    r"\b(manager|director|principal|staff|lead|head\s+of|senior|sr\.?|"
    r"supervisor|coordinator|recruiter)\b", re.I)


def is_internship(title: str, term: str = "") -> bool:
    """Is this a student role? Title first, term as a tiebreak."""
    t = title or ""
    if _NOT_INTERN.search(t):
        return False
    return bool(_INTERN.search(t) or _INTERN.search(term or ""))


class JobQueue:
    """A JSON file of decisions, keyed by job id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, QueueEntry] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt queue must not wipe the candidate's decisions on the
            # next write, so refuse to start from empty.
            raise RuntimeError(f"job queue at {self.path} is unreadable: {exc}") from exc
        for jid, d in raw.items():
            known = {k: v for k, v in d.items() if k in QueueEntry.__annotations__}
            self._entries[jid] = QueueEntry(**{**known, "job_id": jid})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {k: v.to_dict() for k, v in self._entries.items()}, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # -- reading ----------------------------------------------------------

    def all(self) -> list[QueueEntry]:
        """Approved first, then fresh rows by fit, then stale rows by fit.

        Fit alone put a 2021 posting above a last-week one at the same score.
        Roughly 18-22% of postings are ghosts and a stale date is the
        strongest cheap signal, so anything older than STALE_DAYS sinks below
        every fresh row. Undated rows count as fresh: unknown is not evidence.
        """
        return sorted(self._entries.values(), key=_rank)

    def get(self, job_id: str) -> QueueEntry | None:
        return self._entries.get(job_id)

    def approved_ids(self) -> set[str]:
        return {k for k, v in self._entries.items() if v.decision == "approved"}

    def blacklisted_ids(self) -> set[str]:
        return {k for k, v in self._entries.items() if v.decision == "blacklist"}

    def counts(self) -> dict[str, int]:
        out = {d: 0 for d in DECISIONS}
        for v in self._entries.values():
            out[v.decision] = out.get(v.decision, 0) + 1
        return out

    # -- writing ----------------------------------------------------------

    def add_posts(self, posts: Iterable[Any], fits: dict[str, float] | None = None) -> int:
        """Record newly discovered postings as pending. Never touch a decision."""
        fits = fits or {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = 0
        for p in posts:
            jid = p.job_id
            raw = getattr(p, "raw", None) or {}
            terms = raw.get("terms") or ([raw["season"]] if raw.get("season") else [])
            described = dict(
                company=p.company, title=p.title, url=p.url,
                location=p.location or "", ats=p.ats.value,
                posted=p.posted_at.date().isoformat() if p.posted_at else "",
                term=", ".join(str(t) for t in terms)[:40],
                sponsorship=str(raw.get("sponsorship") or "")[:40],
                remote=bool(getattr(p, "remote", False)),
                department=str(getattr(p, "department", ""))[:60],
                source=str(raw.get("source") or p.ats.value)[:30])
            cur = self._entries.get(jid)
            if cur is not None:
                # Refresh what the posting says, keep what the candidate said.
                for k, v in described.items():
                    if v:
                        setattr(cur, k, v)
                if jid in fits:
                    cur.fit = round(fits[jid], 3)
                continue
            self._entries[jid] = QueueEntry(
                job_id=jid, discovered_at=now,
                fit=round(fits.get(jid, 0.0), 3), **described)
            added += 1
        if added or posts:
            self.save()
        return added

    def decide(self, job_id: str, decision: str, *, note: str = "") -> bool:
        if decision not in DECISIONS:
            return False
        cur = self._entries.get(job_id)
        if cur is None:
            cur = self._entries[job_id] = QueueEntry(job_id=job_id)
        cur.decision = decision
        cur.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if note:
            cur.note = note[:300]
        return True

    def keep_only_internships(self) -> tuple[int, int]:
        """Drop every non-student posting. Returns (kept, dropped).

        A decision the candidate made is never dropped: a blacklisted job has
        to stay blacklisted whether or not this classifier likes its title.
        """
        keep, drop = {}, 0
        for jid, x in self._entries.items():
            if x.decision != "pending" or is_internship(x.title, x.term):
                keep[jid] = x
            else:
                drop += 1
        self._entries = keep
        self.save()
        return len(keep), drop

    def decide_many(self, decisions: dict[str, str]) -> int:
        n = sum(1 for jid, d in decisions.items() if self.decide(jid, d))
        if n:
            self.save()
        return n
