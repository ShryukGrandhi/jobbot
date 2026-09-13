"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import structlog
from dotenv import load_dotenv

from jobbot.browser.session import BrowserConfig, BrowserSession
from jobbot.discovery.sources import Discovery, JobPost, ghost_score
from jobbot.llm.client import LLMClient
from jobbot.orchestrator import (
    HaltWithTabOpen, Orchestrator, RunConfig, fit_score,
)
from jobbot.profile import Profile
from jobbot.queue import JobQueue
from jobbot.tracker.csv_tracker import Status, Tracker

log = structlog.get_logger(__name__)
DEFAULT_PROFILE = Path("config/profile.yaml")
DEFAULT_CSV = Path("data/applications.csv")


async def _collect(sources: list[str], limit_per: int) -> list[JobPost]:
    d = Discovery()
    posts: list[JobPost] = []
    for spec in sources:
        kind, _, rest = spec.partition(":")
        try:
            if kind == "greenhouse":
                posts += await d.greenhouse(rest)
            elif kind == "lever":
                posts += await d.lever(rest)
            elif kind == "ashby":
                posts += await d.ashby(rest)
            elif kind == "smartrecruiters":
                posts += await d.smartrecruiters(rest)
            elif kind == "workable":
                posts += await d.workable(rest)
            elif kind == "workday":
                tenant, site, pod = (rest.split("/") + ["wd1"])[:3]
                posts += await d.workday(tenant, site, pod, max_jobs=limit_per)
            elif kind == "interns":
                posts += await d.intern_list(rest or "simplify")
            elif kind == "linkedin":
                # Aggregator rows carry no apply endpoint, so resolve each one to
                # the company's own board before it reaches the queue. The
                # orchestrator refuses anything still unresolved rather than
                # falling back to LinkedIn's own apply flow.
                from jobbot.discovery.aggregator import enrich_with_boards, scrape
                found = await asyncio.to_thread(
                    scrape, rest, results_wanted=max(20, limit_per))
                posts += await enrich_with_boards(found)
            else:
                print(f"unknown source kind: {kind!r}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  {spec}: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
    return posts


def _standard_resume(args, data_dir: Path) -> Path | None:
    """The one PDF to send, unless the run explicitly asks for a tailored one."""
    if args.tailor:
        return None
    p = Path(args.resume).expanduser() if args.resume else data_dir / "standard_resume.pdf"
    if not p.exists():
        if args.resume:
            raise SystemExit(f"no such resume: {p}")
        return None
    print(f"resume: sending {p} to every application (--tailor to generate instead)")
    return p


def cmd_discover(args) -> int:
    posts = asyncio.run(_collect(args.source, args.limit))
    profile = Profile.load(args.profile) if Path(args.profile).exists() else None
    rows = []
    for p in posts:
        rows.append((fit_score(profile, p) if profile else 0.0, ghost_score(p, posts), p))
    # Discovery feeds the dashboard queue, so the candidate can tick jobs at
    # /queue before anything opens a browser. Needs no API key: the queue is a
    # shortlist, not a run.
    queue = JobQueue(Path(args.csv).parent / "queue.json")
    queue.add_posts(posts, {p.job_id: fit for fit, _, p in rows})
    if profile is not None:
        queue.decide_many({p.job_id: "blacklist" for p in posts
                           if profile.excludes(p.company)})
    queue.save()
    rows.sort(key=lambda r: r[0], reverse=True)
    print(f"{'fit':>5} {'ghost':>6}  {'ats':<12} {'company':<18} title")
    for m, g, p in rows[: args.limit]:
        flag = " GHOST?" if g > 0.6 else ""
        print(f"{m:5.2f} {g:6.2f}  {p.ats.value:<12} {p.company[:18]:<18} {p.title[:52]}{flag}")
    print(f"\n{len(posts)} postings from {len(args.source)} source(s)")
    print(f"queue: {queue.counts()}  -- tick jobs at /queue on `jobbot dashboard`, "
          "then `jobbot run --approved`")
    return 0


def cmd_check(args) -> int:
    ok = True
    print("profile:")
    try:
        profile = Profile.load(args.profile)
        print(f"  loaded {profile.identity.full_name or '(no name set)'} "
              f"<{profile.identity.email_str or 'no email set'}>")
        print(f"  {len(profile.experience)} roles, {profile.total_years_experience}y total")
        gaps = profile.employment_gaps()
        if gaps:
            print(f"  employment gaps >6mo: {len(gaps)} -- explain these; ~48% of "
                  "employers auto-screen on them")
        missing = profile.missing_legally_significant()
        if missing:
            ok = False
            print(f"  MISSING legally significant answers ({len(missing)}):")
            for m in missing:
                print(f"    - {m}")
            print("  the bot will refuse to run autonomously until these are set")
        else:
            print("  all legally significant answers confirmed")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  FAILED: {exc}")

    print("\nllm:")
    try:
        c = LLMClient()
        h = c.health()
        print(f"  provider={c.provider} model={c.model} status={h.get('status')}")
        q = c.quota()
        if q:
            for b in q.get("buckets", []):
                print(f"  quota {b['type']}: {b['status']} "
                      f"({round(b.get('utilization', 0) * 100)}% used)")
        print(f"  fallback: {c.fallback}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  FAILED: {exc}")

    print("\ngithub:")
    try:
        from jobbot.ghproj.auth import consent_record, get_identity
        i = get_identity()
        print(f"  {i.login} via {i.source}, scopes={sorted(i.scopes)}")
        print(f"  consent: {'granted' if consent_record() else 'NOT GRANTED (run: jobbot github-auth)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  unavailable: {str(exc)[:120]}")

    print("\ngmail:")
    from jobbot.mail.gmail import CLIENT_SECRET, TOKEN_PATH
    print(f"  client secret: {'present' if CLIENT_SECRET.exists() else 'MISSING ' + str(CLIENT_SECRET)}")
    print(f"  token:         {'present' if TOKEN_PATH.exists() else 'not yet authorized'}")

    print("\ntracker:")
    t = Tracker(args.csv)
    print(f"  {args.csv}: {t.stats()}")
    return 0 if ok else 1


def cmd_github_auth(args) -> int:
    from jobbot.ghproj.auth import ensure_consent, get_identity
    i = get_identity()
    rec = ensure_consent(i, interactive=True, public=not args.private)
    print(json.dumps(rec, indent=2))
    return 0


def cmd_run(args) -> int:
    profile = Profile.load(args.profile)
    posts = asyncio.run(_collect(args.source, 200))
    tracker = Tracker(args.csv)

    data_dir = Path(args.csv).parent
    queue = JobQueue(data_dir / "queue.json")
    queue.add_posts(posts, {p.job_id: fit_score(profile, p) for p in posts})
    # An excluded company is a standing "never": record it as a decision so it
    # is visible on the queue page rather than silently filtered at run time.
    queue.decide_many({p.job_id: "blacklist" for p in posts
                       if profile.excludes(p.company)})

    # The queue is the candidate's decision, so it outranks anything discovery
    # or the fit filter thinks. Blacklisted jobs are ones being applied to by
    # hand; sending a second application would be worse than sending none.
    blacklisted = queue.blacklisted_ids()
    if blacklisted:
        posts = [p for p in posts if p.job_id not in blacklisted]
    if args.approved:
        ok = queue.approved_ids()
        posts = [p for p in posts if p.job_id in ok]
        print(f"queue: {len(posts)} approved job(s) of {sum(queue.counts().values())} known")
        if not posts:
            print("nothing approved yet -- tick jobs at /queue on the dashboard")
            return 0

    standard = _standard_resume(args, data_dir)
    llm = LLMClient()   # after the queue gate: "nothing approved" needs no key

    cfg = RunConfig(
        # Everything a run writes -- audit dirs, answers.csv, lessons.jsonl --
        # lives beside the tracker. Defaulting to a bare "data" meant --csv moved
        # the index but every artifact still landed in ./data of the cwd.
        data_dir=Path(args.csv).parent,
        dry_run=not args.submit,
        standard_resume=standard,
        make_github_project=not args.no_project,
        publish_project_private=args.private_projects,
        min_match_score=args.min_match,
        # An explicit tick per job outranks the anti-spam cap: the candidate
        # approved nine roles at one company one at a time, and silently
        # dropping six of them would be the software overriding the person.
        per_company_cap=10_000 if args.approved else RunConfig.per_company_cap,
        persist_until_submitted=args.persist,
        max_heal_rounds=12 if args.persist else RunConfig.max_heal_rounds,
        attempts_per_job=args.attempts,
    )

    async def go():
        session = BrowserSession(BrowserConfig(headless=False))
        await session.start()
        try:
            orch = Orchestrator(profile, session, llm, tracker, cfg)
            results = await orch.run(posts, limit=args.limit)
            kept = session.kept_tabs
            if kept:
                print(f"\n{len(kept)} application(s) left OPEN in the browser -- "
                      "filled, not submitted, waiting on you:")
                for k in kept:
                    print(f"  - {k}")
                print("\nEach needs an answer only you can give (see blockers.txt in "
                      "its audit dir). Fill it in the tab, or set it at /edit on the "
                      "dashboard and re-run. Ctrl-C closes the browser.")
                await asyncio.Event().wait()
            return results
        except HaltWithTabOpen as halt:
            print(f"\nHALTED on {halt.job_id} with the tab still open:")
            for b in halt.blockers:
                print(f"  - {b.label[:70]}: {b.problem[:150]}")
            print("\nThe form is filled and live. Nothing was submitted and "
                  "nothing was discarded.")
            await asyncio.Event().wait()
        finally:
            if args.keep_open:
                # The window is the only way to inspect what was actually
                # entered while the page is still live; screenshots come after
                # the fact. Leaving it up holds the profile lock, so the next
                # run cannot start until this window is closed.
                print("\n--keep-open: browser left running. "
                      "Close the window before the next run.")
                await asyncio.Event().wait()
            await session.close()

    results = asyncio.run(go())
    print(f"\n{'job':<34} {'status':<16} detail")
    for r in results:
        print(f"{r.job_id[:34]:<34} {r.status:<16} {r.reason[:60]}")
    for r in results:
        if r.status in (Status.SUBMITTED.value, Status.CONFIRMED.value):
            queue.decide(r.job_id, "applied")
    queue.save()
    print(f"\ntracker: {tracker.stats()}")
    if cfg.dry_run:
        print("\nDRY RUN: forms were filled and verified but NOT submitted. "
              "Re-run with --submit to send them.")
    return 0


def cmd_ats_test(args) -> int:
    """Put a generated resume in front of a real ATS parser."""
    from jobbot.resume.ats_validate import live_lever, offline_check, print_report

    profile = Profile.load(args.profile)
    i = profile.identity
    expect = {
        "name": i.full_name, "email": i.email_str,
        "phone": re.sub(r"[^0-9]", "", i.phone or "")[-10:],
        "location": i.location.city,
        "company": profile.experience[0].company if profile.experience else "",
    }
    expect = {k: v for k, v in expect.items() if v}

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"no such pdf: {pdf}", file=sys.stderr)
        return 1

    print_report(offline_check(pdf, expect))

    if args.lever_url:
        async def go():
            session = BrowserSession(BrowserConfig(headless=False))
            await session.start()
            try:
                async with session.tab("atstest") as page:
                    return await live_lever(page, args.lever_url, pdf, expect)
            finally:
                await session.close()
        print_report(asyncio.run(go()))
    return 0


def cmd_report(args) -> int:
    """Every answer entered for one application, field by field."""
    from jobbot.report import report

    d = Path(args.audit_dir)
    if not d.is_dir():
        print(f"no such audit dir: {d}", file=sys.stderr)
        return 1
    print(report(d))
    return 0


def cmd_dashboard(args) -> int:
    """Local read-only web view of everything on disk."""
    from jobbot.dashboard import serve

    serve(Path(args.csv).parent, args.profile,
          port=args.port, open_browser=not args.no_open,
          tailscale=args.tailscale, host=args.host)
    return 0


def cmd_ui(args) -> int:
    """The brutalist UI: dashboard, queue, intake, editor and a live browser."""
    from jobbot.ui.server import serve

    serve(Path(args.csv).parent, args.profile, port=args.port,
          open_browser=not args.no_open, host=args.host)
    return 0


def cmd_stats(args) -> int:
    t = Tracker(args.csv)
    s = t.stats()
    total = s.pop("total", 0)
    print(f"{args.csv}  ({total} rows)")
    for k, v in sorted(s.items(), key=lambda x: -x[1]):
        print(f"  {k:<18} {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # .env is where SETUP.md tells people to put their keys. Nothing else
    # reads it; without this line every documented key is silently ignored.
    load_dotenv()
    p = argparse.ArgumentParser(prog="jobbot", description="Autonomous job application agent")
    p.add_argument("--profile", default=str(DEFAULT_PROFILE))
    p.add_argument("--csv", default=str(DEFAULT_CSV))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="find and rank jobs, no browser")
    d.add_argument("--source", action="append", required=True,
                   help="greenhouse:slug | lever:slug | ashby:slug | "
                        "smartrecruiters:slug | workable:slug | "
                        "workday:tenant/site/pod | linkedin:search terms")
    d.add_argument("--limit", type=int, default=40)
    d.set_defaults(func=cmd_discover)

    c = sub.add_parser("check", help="verify profile, llm, github, gmail, tracker")
    c.set_defaults(func=cmd_check)

    g = sub.add_parser("github-auth", help="grant consent to create repos")
    g.add_argument("--private", action="store_true")
    g.set_defaults(func=cmd_github_auth)

    r = sub.add_parser("run", help="apply to jobs")
    r.add_argument("--source", action="append", required=True)
    r.add_argument("--limit", type=int, default=5)
    r.add_argument("--submit", action="store_true", help="actually submit (default: dry run)")
    r.add_argument("--approved", action="store_true",
                   help="only apply to jobs ticked on the dashboard queue")
    r.add_argument("--resume", help="PDF to send to every application "
                                    "(default: data/standard_resume.pdf if present)")
    r.add_argument("--tailor", action="store_true",
                   help="generate a resume per application instead of sending the standard one")
    r.add_argument("--attempts", type=int, default=0,
                   help="max tries per application, retried in the same tab "
                        "with backoff (default 0 = keep trying until it settles, "
                        "max 25)")
    r.add_argument("--persist", action="store_true",
                   help="work one application until it submits; on failure stop "
                        "with the tab open instead of moving on")
    r.add_argument("--keep-open", action="store_true",
                   help="leave the browser open after the run instead of closing it")
    r.add_argument("--no-project", action="store_true")
    r.add_argument("--private-projects", action="store_true")
    r.add_argument("--min-match", type=float, default=0.5)
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("ats-test", help="score a resume against real ATS parsers")
    a.add_argument("--pdf", required=True)
    a.add_argument("--lever-url", help="a jobs.lever.co/<co>/<id>/apply URL for a live parse test")
    a.set_defaults(func=cmd_ats_test)

    rp = sub.add_parser("report", help="every answer entered, field by field")
    rp.add_argument("audit_dir", help="data/applications/<job_id>/")
    rp.set_defaults(func=cmd_report)

    dash = sub.add_parser("dashboard", help="local web view of every run")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--no-open", action="store_true", help="do not open a browser")
    dash.add_argument("--tailscale", action="store_true",
                      help="bind the Tailscale address so other devices on your "
                           "tailnet can reach it (never 0.0.0.0)")
    dash.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    dash.set_defaults(func=cmd_dashboard)

    ui = sub.add_parser("ui", help="the UI: queue, intake, editor, answers and a live browser")
    ui.add_argument("--port", type=int, default=8766)
    ui.add_argument("--no-open", action="store_true", help="do not open a browser")
    ui.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    ui.set_defaults(func=cmd_ui)

    s = sub.add_parser("stats", help="summarize the tracker")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
