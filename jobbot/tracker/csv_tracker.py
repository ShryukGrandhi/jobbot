"""Human-readable application ledger.

CSV on purpose: the user asked for it, and it is the right call. This file is
the record of what was said, in the user's name, to real employers. It must be
openable in any spreadsheet, greppable, diffable, and repairable by hand without
a migration tool.

Durability is handled with an atomic replace plus an exclusive lock, because the
one thing worse than no ledger is a truncated one. Every write rewrites the
whole file -- fine at the scale of a job search (hundreds of rows, not millions).
"""

from __future__ import annotations

import contextlib
import csv
import enum
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import structlog

from jobbot.tracker.filelock import exclusive

log = structlog.get_logger(__name__)


class Status(str, enum.Enum):
    DISCOVERED = "discovered"        # found, not yet evaluated
    FILTERED_OUT = "filtered_out"    # failed the fit filter, never touched
    GHOST_SUSPECTED = "ghost_suspected"
    KNOCKOUT_FAIL = "knockout_fail"  # an honest answer triggers auto-reject
    UNREACHABLE = "unreachable"      # form needs an account we could not make
    PREPARED = "prepared"            # resume + project built, ready to fill
    FILLING = "filling"
    NEEDS_HUMAN = "needs_human"      # missing a legally significant answer
    SUBMITTED = "submitted"          # submit clicked
    CONFIRMED = "confirmed"          # independently observed confirmation
    FAILED = "failed"


@dataclass
class Application:
    # identity
    job_id: str = ""                 # "{ats}:{native_id}" -- stable global key
    company: str = ""
    title: str = ""
    ats: str = ""
    job_url: str = ""
    apply_url: str = ""
    location: str = ""
    remote: str = ""
    posted_at: str = ""
    salary_posted: str = ""
    source: str = ""

    # evaluation
    status: str = Status.DISCOVERED.value
    match_score: str = ""            # 0-1, fit against the profile
    ghost_score: str = ""            # 0-1, higher == likelier to be a ghost job
    knockout_reason: str = ""

    # artifacts
    resume_path: str = ""
    cover_letter_path: str = ""
    github_project_url: str = ""
    screenshots_dir: str = ""
    audit_dir: str = ""

    # outcome
    discovered_at: str = ""
    applied_at: str = ""
    questions_total: str = ""
    questions_answered: str = ""
    questions_flagged: str = ""
    heal_rounds: str = ""
    confirmation_text: str = ""
    error: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.discovered_at:
            self.discovered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


COLUMNS = [f.name for f in fields(Application)]


class Tracker:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_all([])

    # -- locking ----------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        with exclusive(self.path.with_suffix(".lock")):
            yield

    # -- io ---------------------------------------------------------------

    def _read_all(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with open(self.path, newline="", encoding="utf-8") as fh:
            return [dict(r) for r in csv.DictReader(fh)]

    def _write_all(self, rows: list[dict[str, str]]) -> None:
        """Atomic: write a temp file in the same dir, fsync, then rename."""
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({c: r.get(c, "") for c in COLUMNS})
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- api --------------------------------------------------------------

    def all(self) -> list[Application]:
        return [Application(**{k: v for k, v in r.items() if k in COLUMNS})
                for r in self._read_all()]

    def get(self, job_id: str) -> Application | None:
        for r in self._read_all():
            if r.get("job_id") == job_id:
                return Application(**{k: v for k, v in r.items() if k in COLUMNS})
        return None

    def seen(self, job_id: str) -> bool:
        return any(r.get("job_id") == job_id for r in self._read_all())

    def already_applied(self, job_id: str) -> bool:
        """Terminal states. Guards against ever double-submitting to a human."""
        row = self.get(job_id)
        return row is not None and row.status in (
            Status.SUBMITTED.value, Status.CONFIRMED.value
        )

    def applied_companies(self) -> set[str]:
        return {
            r.company.strip().lower()
            for r in self.all()
            if r.status in (Status.SUBMITTED.value, Status.CONFIRMED.value) and r.company
        }

    def upsert(self, app: Application) -> None:
        with self._lock():
            rows = self._read_all()
            payload = {k: ("" if v is None else str(v)) for k, v in asdict(app).items()}
            for i, r in enumerate(rows):
                if r.get("job_id") == app.job_id:
                    merged = {**r, **{k: v for k, v in payload.items() if v != ""}}
                    rows[i] = merged
                    break
            else:
                rows.append(payload)
            self._write_all(rows)
        log.debug("tracker.upsert", job_id=app.job_id, status=app.status)

    def update(self, job_id: str, **changes: object) -> None:
        with self._lock():
            rows = self._read_all()
            for r in rows:
                if r.get("job_id") == job_id:
                    for k, v in changes.items():
                        if k in COLUMNS:
                            r[k] = "" if v is None else str(v)
                    break
            else:
                raise KeyError(f"no such job_id in tracker: {job_id}")
            self._write_all(rows)
        log.debug("tracker.update", job_id=job_id, **{k: str(v)[:40] for k, v in changes.items()})

    def mark_submitted(self, job_id: str, confirmation: str = "") -> None:
        self.update(
            job_id,
            status=Status.CONFIRMED.value if confirmation else Status.SUBMITTED.value,
            applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            confirmation_text=confirmation[:500],
        )

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.all():
            out[r.status] = out.get(r.status, 0) + 1
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out
