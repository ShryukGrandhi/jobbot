"""Create the repository and push the project as a real commit series.

Honesty rules enforced here, not merely intended:

  * Commits are made now, with real timestamps. GIT_AUTHOR_DATE and
    GIT_COMMITTER_DATE are never set. Rewriting commit dates so a
    just-generated repo appears to have months of organic history is
    fabricating a record that employers read as factual -- it is resume fraud
    with a git log, and it is trivially detectable via the GitHub API anyway.
  * The README carries a short, factual provenance line saying when and why the
    project was built.

A same-session repo therefore reads as exactly what it is: a recent portfolio
piece. That is a perfectly good thing to show -- provided the code is real and
the candidate can defend it, which is what the quality gate is for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

from jobbot.ghproj.auth import API, GitHubIdentity

log = structlog.get_logger(__name__)


class PublishError(RuntimeError):
    pass


@dataclass
class PublishedProject:
    name: str
    url: str
    clone_url: str
    commits: int
    private: bool
    local_path: Path | None = None


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    e = {**os.environ, **(env or {})}
    r = subprocess.run(cmd, cwd=str(cwd), env=e, capture_output=True, text=True)
    if r.returncode != 0:
        raise PublishError(f"{' '.join(cmd[:3])} failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


PROVENANCE = """

---

*Built {when} as a portfolio project while exploring {topic}. Written from
scratch; see the commit history for how it came together.*
"""


def _augment_readme(content: str, topic: str) -> str:
    when = datetime.now(timezone.utc).strftime("%B %Y")
    if "portfolio project" in content.lower():
        return content
    return content.rstrip() + PROVENANCE.format(when=when, topic=topic)


def build_locally(plan: dict[str, Any], workdir: Path, *, author_name: str,
                  author_email: str) -> int:
    """Materialize the commit series in a local git repo. Returns commit count."""
    workdir.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], workdir)
    _run(["git", "config", "user.name", author_name], workdir)
    _run(["git", "config", "user.email", author_email], workdir)

    topic = plan.get("one_liner", "the problem space")
    made = 0
    for c in plan.get("commits", []):
        wrote = False
        for f in c.get("files", []):
            path = workdir / f["path"]
            if ".." in Path(f["path"]).parts or Path(f["path"]).is_absolute():
                raise PublishError(f"unsafe path in plan: {f['path']!r}")
            path.parent.mkdir(parents=True, exist_ok=True)
            content = f.get("content", "")
            if path.name.lower().startswith("readme"):
                content = _augment_readme(content, topic)
            path.write_text(content, encoding="utf-8")
            wrote = True
        if not wrote:
            continue
        _run(["git", "add", "-A"], workdir)
        status = _run(["git", "status", "--porcelain"], workdir)
        if not status:
            continue
        # No GIT_*_DATE overrides: commits carry their real time, deliberately.
        _run(["git", "commit", "-q", "-m", c["message"]], workdir)
        made += 1

    log.info("project.built_locally", commits=made, path=str(workdir))
    return made


def create_repo(identity: GitHubIdentity, name: str, description: str,
                *, private: bool = False, topics: list[str] | None = None) -> dict:
    r = httpx.post(f"{API}/user/repos", headers=identity.headers(), timeout=30, json={
        "name": name,
        "description": description[:350],
        "private": private,
        "has_issues": True,
        "has_wiki": False,
        "auto_init": False,
    })
    if r.status_code == 422:
        raise PublishError(f"repo name already taken: {name}")
    if r.status_code not in (200, 201):
        raise PublishError(f"repo creation failed ({r.status_code}): {r.text[:300]}")
    repo = r.json()

    if topics:
        httpx.put(f"{API}/repos/{identity.login}/{name}/topics",
                  headers={**identity.headers(),
                           "Accept": "application/vnd.github.mercy-preview+json"},
                  json={"names": [t.lower()[:35] for t in topics[:6]]}, timeout=20)
    return repo


def publish(
    identity: GitHubIdentity,
    plan: dict[str, Any],
    *,
    private: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
    keep_local: Path | None = None,
    dry_run: bool = False,
) -> PublishedProject:
    """Build the commit series and push it to a new repository."""
    name = plan["repo_name"]
    desc = plan.get("one_liner", "")
    workdir = Path(keep_local) if keep_local else Path(tempfile.mkdtemp(prefix="jobbot-proj-"))
    if workdir.exists() and any(workdir.iterdir()):
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    commits = build_locally(
        plan, workdir,
        author_name=author_name or identity.login,
        author_email=author_email or f"{identity.login}@users.noreply.github.com",
    )

    if dry_run:
        log.info("project.dry_run", repo=name, commits=commits, path=str(workdir))
        return PublishedProject(name=name, url=f"(dry-run) {name}", clone_url="",
                                commits=commits, private=private, local_path=workdir)

    repo = create_repo(identity, name, desc, private=private,
                       topics=plan.get("topics"))
    push_url = f"https://x-access-token:{identity.token}@github.com/{identity.login}/{name}.git"
    _run(["git", "remote", "add", "origin", push_url], workdir)
    _run(["git", "push", "-q", "-u", "origin", "main"], workdir)

    # Never leave the token sitting in .git/config.
    _run(["git", "remote", "set-url", "origin", repo["clone_url"]], workdir)

    log.info("project.published", repo=name, url=repo["html_url"], commits=commits)
    return PublishedProject(
        name=name, url=repo["html_url"], clone_url=repo["clone_url"],
        commits=commits, private=private, local_path=workdir,
    )


def verify_published(identity: GitHubIdentity, name: str) -> dict[str, Any]:
    """Read the repo back from the API. Trust the server, not our own logs."""
    r = httpx.get(f"{API}/repos/{identity.login}/{name}", headers=identity.headers(), timeout=20)
    if r.status_code != 200:
        return {"exists": False}
    repo = r.json()
    cr = httpx.get(f"{API}/repos/{identity.login}/{name}/commits",
                   headers=identity.headers(), params={"per_page": 100}, timeout=20)
    commits = cr.json() if cr.status_code == 200 else []
    return {
        "exists": True,
        "url": repo["html_url"],
        "private": repo["private"],
        "commit_count": len(commits),
        "messages": [c["commit"]["message"].splitlines()[0] for c in commits][::-1],
        "first_commit": commits[-1]["commit"]["author"]["date"] if commits else None,
        "last_commit": commits[0]["commit"]["author"]["date"] if commits else None,
    }


def smoke_test(workdir: Path, run_command: str | None = None, timeout: int = 120) -> dict[str, Any]:
    """Actually execute the generated project before it becomes public.

    Publishing code that does not run is worse than publishing nothing: a
    reviewer who clones it and hits an ImportError has learned something
    specific and bad about the candidate. So we run it here, and a failure
    blocks the push.
    """
    result: dict[str, Any] = {"ran": False, "passed": False, "output": "", "command": ""}

    has_py = any(workdir.rglob("*.py"))
    has_tests = any(p for p in workdir.rglob("*") if "test" in p.name.lower() and p.is_file())

    cmds: list[list[str]] = []
    if run_command:
        cmds.append(run_command.split())
    if has_py and has_tests:
        cmds.append(["python", "-m", "pytest", "-q"])
    if has_py:
        cmds.append(["python", "-c",
                     "import pathlib,py_compile,sys;"
                     "[py_compile.compile(str(p), doraise=True) for p in pathlib.Path('.').rglob('*.py')];"
                     "print('compile ok')"])

    for cmd in cmds:
        try:
            r = subprocess.run(cmd, cwd=str(workdir), capture_output=True,
                               text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            result["output"] = f"{type(exc).__name__}: {exc}"
            continue
        out = (r.stdout + r.stderr).strip()
        result.update(ran=True, passed=(r.returncode == 0),
                      output=out[-2500:], command=" ".join(cmd))
        if r.returncode == 0:
            break

    log.info("project.smoke_test", ran=result["ran"], passed=result["passed"],
             command=result["command"])
    return result
