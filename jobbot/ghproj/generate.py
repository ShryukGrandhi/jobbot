"""Design a small, real project targeted at one specific role and team.

The signalling logic, stated plainly so the code does not drift from it:

A portfolio artifact is worth something only when it is expensive to fake and
cheap to verify. Repo count, green squares, and a scaffolded demo are none of
those -- they are free to produce, so they carry no information. What carries
information is working code that solves a real problem in the team's actual
domain, with a README that documents a decision and its tradeoff.

Two honesty constraints are load-bearing:

  * The project is generated NOW and its commits carry real timestamps. It is a
    recent portfolio piece and is described as one. Commit dates are never
    rewritten to manufacture months of history -- that is fabricating a record
    an employer reads as factual.
  * It goes in the Projects section, never Experience. It is not a job.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from jobbot.llm.client import LLMClient, cached_system
from jobbot.profile import Profile

log = structlog.get_logger(__name__)

PROJECT_TOOL: dict[str, Any] = {
    "name": "design_project",
    "description": (
        "Design a small but genuinely useful project targeted at this role and team, "
        "built from technologies the candidate already knows. Return complete, "
        "runnable file contents and an incremental commit plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "repo_name": {
                "type": "string",
                "description": "Lowercase, hyphenated, memorable, descriptive of what it does. Not the company name.",
            },
            "one_liner": {"type": "string", "description": "Under 90 chars. GitHub repo description."},
            "why_this_role": {
                "type": "string",
                "description": "How this maps to the team's actual work. For the human, not the resume.",
            },
            "language": {"type": "string"},
            "topics": {"type": "array", "items": {"type": "string"}, "description": "3-6 GitHub topics."},
            "design_tradeoff": {
                "type": "string",
                "description": "A real engineering decision with a real cost, and why it was chosen. This is what makes the README worth reading.",
            },
            "commits": {
                "type": "array",
                "description": (
                    "5 to 7 commits, each a coherent incremental step a person would "
                    "actually make: scaffold, then core types, then the algorithm, then "
                    "CLI, then tests, then hardening, then docs. Each lists the files it "
                    "writes with their FULL contents at that point."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Imperative, specific, lowercase after any prefix. No 'initial commit' beyond the first.",
                        },
                        "files": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string", "description": "COMPLETE file contents as of this commit."},
                                },
                                "required": ["path", "content"],
                            },
                        },
                    },
                    "required": ["message", "files"],
                },
            },
            "resume_bullets": {
                "type": "array", "items": {"type": "string"},
                "description": "1-2 factual bullets for the resume's Projects section. Describe only what the code does.",
            },
            "run_command": {"type": "string", "description": "How to run the tests, e.g. 'python -m pytest'."},
        },
        "required": ["repo_name", "one_liner", "language", "commits", "resume_bullets", "design_tradeoff"],
    },
}

SYSTEM = """You design small, real, working software projects for a specific job application.

Quality bar -- a senior engineer on the hiring team will open this repo:
- It must actually run. Complete files, correct imports, no TODO stubs, no pseudocode.
- It must solve a real problem in the team's domain, not a toy exercise.
- Include real tests that assert real behaviour, including at least one edge case.
- The README must document a genuine design tradeoff: what you chose, what it cost,
  and what you would do differently at 100x scale. This is the single most valuable
  part of the repo and the hardest thing to fake.
- Handle errors. Validate input. A reviewer looks for this immediately.
- Small and complete beats large and half-finished. 150-350 lines across AT MOST
  8 files is right. A focused project reads as deliberate; a sprawling one does not.
- Use ONE language. Do not add a second stack for its own sake.

Use ONLY technologies from the candidate's profile. Do not showcase a language they
do not know -- they have to defend this in an interview.

Commit plan: 6-9 commits that tell the story of building it, each self-consistent.
A file's content in a later commit supersedes the earlier one. Do not write a commit
that leaves the tree broken.

Never reference the company name in code or the repo name. This is the candidate's
project, not a pitch deck."""


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", s or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")[:60] or "project"


def design_project(
    llm: LLMClient,
    profile: Profile,
    *,
    job_title: str,
    company: str,
    job_description: str,
    team_context: str = "",
) -> dict[str, Any]:
    from jobbot.healer.answer import profile_digest

    r = llm.call(
        system=cached_system(SYSTEM + "\n\nCANDIDATE PROFILE:\n" + profile_digest(profile)),
        blocks=[{"type": "text", "text":
                 f"ROLE: {job_title}\nCOMPANY: {company}\n"
                 f"{('TEAM CONTEXT: ' + team_context) if team_context else ''}\n\n"
                 f"JOB DESCRIPTION:\n{job_description[:10000]}\n\n"
                 "Design the project. Every file must be complete and runnable."}],
        tool=PROJECT_TOOL,
        max_tokens=10000,
    )
    d = r.require_tool()
    d["repo_name"] = slugify(d.get("repo_name", ""))
    log.info("project.designed", repo=d["repo_name"], lang=d.get("language"),
             commits=len(d.get("commits", [])),
             files=sum(len(c.get("files", [])) for c in d.get("commits", [])))
    return d


def validate_plan(plan: dict[str, Any]) -> list[str]:
    """Reject a plan that would produce an embarrassing repo."""
    problems: list[str] = []
    commits = plan.get("commits", [])

    if not (5 <= len(commits) <= 12):
        problems.append(f"commit count {len(commits)} outside the 5-12 range")

    all_paths: set[str] = set()
    total_bytes = 0
    for i, c in enumerate(commits):
        if not c.get("files"):
            problems.append(f"commit {i+1} ({c.get('message','')[:40]!r}) writes no files")
        for f in c.get("files", []):
            all_paths.add(f["path"])
            total_bytes += len(f.get("content", ""))
            if len(f.get("content", "").strip()) < 8:
                problems.append(f"file {f['path']!r} in commit {i+1} is effectively empty")

    if not any(p.lower().startswith("readme") for p in all_paths):
        problems.append("no README")
    if not any(("test" in p.lower() or "spec" in p.lower()) for p in all_paths):
        problems.append("no tests")
    if total_bytes < 1500:
        problems.append(f"project is too small to be credible ({total_bytes} bytes)")

    blob = json.dumps(plan).lower()
    # NB: a bare "..." is NOT a placeholder -- it is Python's Ellipsis and JS
    # spread syntax, and flagging it rejects perfectly good real code. Match the
    # shapes that actually indicate unfinished work.
    markers = (
        "todo:", "todo(", "fixme", "your code here", "not implemented",
        "notimplementederror", "lorem ipsum", "<placeholder>",
        "# ... rest", "// ... rest", "# ...rest", "implement this",
        "add logic here", "pass  # stub", "xxx:",
    )
    for marker in markers:
        if marker in blob:
            problems.append(f"placeholder marker present: {marker!r}")

    return problems
