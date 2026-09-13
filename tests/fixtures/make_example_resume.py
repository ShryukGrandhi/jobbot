"""Regenerate tests/fixtures/example_resume.pdf.

A single-column, text-layer resume for Jane Doe -- the same person as
config/profile.example.yaml and example_dump.txt, deliberately worded a little
differently from the dictation so the merge has something real to reconcile.
"""
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

LINES = [
    ("JANE DOE", 15),
    ("San Francisco, CA 94107 | jane.doe@example.com | (555) 123-4567", 9),
    ("linkedin.com/in/janedoe | github.com/janedoe", 9),
    ("", 9),
    ("EXPERIENCE", 11),
    ("Example Corp - Software Engineer, Jun 2023 - Present, San Francisco CA", 10),
    ("- Rebuilt ingestion path; cut p99 write latency 840ms -> 95ms", 10),
    ("- Migrated 38 services with zero customer-visible downtime", 10),
    ("- On-call lead for the storage tier; wrote the runbook still in use", 10),
    ("Tiny Startup - Full Stack Engineer, Oct 2021 - May 2023", 10),
    ("- Built customer dashboard (React, Django REST) used daily by ~400 users", 10),
    ("", 9),
    ("EDUCATION", 11),
    ("Example University - B.S. Computer Science, expected May 2027, GPA 3.7", 10),
    ("", 9),
    ("PROJECTS", 11),
    ("example-project - FastAPI service, github.com/janedoe/example-project", 10),
    ("", 9),
    ("SKILLS", 11),
    ("Python, Go, TypeScript, SQL, PostgreSQL, Docker, Redis, Linux, FastAPI, React, Next.js", 10),
    ("", 9),
    ("AWARDS", 11),
    ("2nd place, ICPC Regional 2025", 10),
]


def main(out: Path) -> None:
    c = canvas.Canvas(str(out), pagesize=letter)
    y = 750
    for text, size in LINES:
        c.setFont("Helvetica-Bold" if size >= 11 else "Helvetica", size)
        c.drawString(60, y, text)
        y -= size + 6
    c.save()


if __name__ == "__main__":
    dest = Path(__file__).with_name("example_resume.pdf")
    main(dest)
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")
