"""Per-answer ledger: one row per (application, field, answer).

`applications.csv` is the index -- one row per job. This is the detail: every
single thing said on your behalf, in a shape a spreadsheet can filter and sort.

Kept as a separate file on purpose. Folding a whole answer set into one cell of
the application row makes both unreadable; this way you can sort by field label
across every application, or filter to just the answers that were left blank.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from jobbot.tracker.filelock import exclusive

log = structlog.get_logger(__name__)

COLUMNS = [
    "recorded_at", "job_id", "company", "title", "ats", "job_url",
    "field_label", "field_kind", "required", "answer", "source",
    "confidence", "rationale", "left_blank", "blank_reason",
]


class AnswerLog:
    def __init__(self, path: str | Path = "data/answers.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with open(self.path, newline="", encoding="utf-8") as fh:
            return [dict(r) for r in csv.DictReader(fh)]

    def _write(self, rows: list[dict[str, str]]) -> None:
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
            with suppress(OSError):
                os.unlink(tmp)
            raise

    def record(self, *, job_id: str, company: str, title: str, ats: str,
               job_url: str, answers: list[dict[str, Any]]) -> int:
        """Append this application's answers, replacing any earlier attempt."""
        lock = self.path.with_suffix(".lock")
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with exclusive(lock):
            rows = [r for r in self._read() if r.get("job_id") != job_id]
            for a in answers:
                val = a.get("value")
                rows.append({
                    "recorded_at": stamp,
                    "job_id": job_id, "company": company, "title": title,
                    "ats": ats, "job_url": job_url,
                    "field_label": (a.get("label") or a.get("field_id") or "")[:300],
                    "field_kind": a.get("kind", ""),
                    "required": str(bool(a.get("required"))),
                    "answer": "" if val is None else str(val)[:4000],
                    "source": a.get("source", ""),
                    "confidence": str(a.get("confidence", "")),
                    "rationale": (a.get("rationale") or "")[:500],
                    "left_blank": str(bool(a.get("needs_human"))),
                    "blank_reason": (a.get("blocked_reason") or "")[:300],
                })
            self._write(rows)
        log.info("answers_csv.recorded", job_id=job_id, rows=len(answers))
        return len(answers)

    def for_job(self, job_id: str) -> list[dict[str, str]]:
        return [r for r in self._read() if r.get("job_id") == job_id]

    def stats(self) -> dict[str, int]:
        rows = self._read()
        return {
            "total_answers": len(rows),
            "applications": len({r["job_id"] for r in rows}),
            "left_blank": sum(1 for r in rows if r.get("left_blank") == "True"),
        }


def backfill(audit_root: str | Path = "data/applications",
             tracker_csv: str | Path = "data/applications.csv",
             out: str | Path = "data/answers.csv") -> int:
    """Rebuild answers.csv from audit directories already on disk."""
    from jobbot.tracker.csv_tracker import Tracker

    idx = {a.job_id: a for a in Tracker(tracker_csv).all()}
    alog = AnswerLog(out)
    n = 0
    for d in sorted(Path(audit_root).glob("*")):
        f = d / "answers.json"
        if not f.exists():
            continue
        job_id = d.name.replace("_", ":", 1)
        row = idx.get(job_id)
        try:
            answers = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        n += alog.record(
            job_id=job_id,
            company=row.company if row else "",
            title=row.title if row else "",
            ats=row.ats if row else job_id.split(":")[0],
            job_url=row.job_url if row else "",
            answers=answers)
    return n
