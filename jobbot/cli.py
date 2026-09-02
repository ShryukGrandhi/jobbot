"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import structlog

from jobbot.browser.session import BrowserConfig, BrowserSession
from jobbot.discovery.sources import Discovery, JobPost, ghost_score
from jobbot.llm.client import LLMClient
from jobbot.orchestrator import Orchestrator, RunConfig, fit_score
from jobbot.profile import Profile
from jobbot.tracker.csv_tracker import Tracker

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
            else:
                print(f"unknown source kind: {kind!r}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"  {spec}: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
    return posts


def cmd_discover(args) -> int:
    posts = asyncio.run(_collect(args.source, args.limit))
    profile = Profile.load(args.profile) if Path(args.profile).exists() else None
    rows = []
    for p in posts:
        rows.append((fit_score(profile, p) if profile else 0.0, ghost_score(p, posts), p))
    rows.sort(key=lambda r: r[0], reverse=True)
    print(f"{'fit':>5} {'ghost':>6}  {'ats':<12} {'company':<18} title")
    for m, g, p in rows[: args.limit]:
        flag = " GHOST?" if g > 0.6 else ""
        print(f"{m:5.2f} {g:6.2f}  {p.ats.value:<12} {p.company[:18]:<18} {p.title[:52]}{flag}")
    print(f"\n{len(posts)} postings from {len(args.source)} source(s)")
    return 0


def cmd_check(args) -> int:
    ok = True
    print("profile:")
    try:
        profile = Profile.load(args.profile)
        print(f"  loaded {profile.identity.full_name} <{profile.identity.email}>")
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
    llm = LLMClient()

    cfg = RunConfig(
        dry_run=not args.submit,
        make_github_project=not args.no_project,
        publish_project_private=args.private_projects,
        min_match_score=args.min_match,
    )

    async def go():
        session = BrowserSession(BrowserConfig(headless=False))
        await session.start()
        try:
            orch = Orchestrator(profile, session, llm, tracker, cfg)
            return await orch.run(posts, limit=args.limit)
        finally:
            await session.close()

    results = asyncio.run(go())
    print(f"\n{'job':<34} {'status':<16} detail")
    for r in results:
        print(f"{r.job_id[:34]:<34} {r.status:<16} {r.reason[:60]}")
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
        "name": i.full_name, "email": str(i.email),
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


def cmd_stats(args) -> int:
    t = Tracker(args.csv)
    s = t.stats()
    total = s.pop("total", 0)
    print(f"{args.csv}  ({total} rows)")
    for k, v in sorted(s.items(), key=lambda x: -x[1]):
        print(f"  {k:<18} {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jobbot", description="Autonomous job application agent")
    p.add_argument("--profile", default=str(DEFAULT_PROFILE))
    p.add_argument("--csv", default=str(DEFAULT_CSV))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="find and rank jobs, no browser")
    d.add_argument("--source", action="append", required=True,
                   help="greenhouse:slug | lever:slug | ashby:slug | workday:tenant/site/pod")
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
    r.add_argument("--no-project", action="store_true")
    r.add_argument("--private-projects", action="store_true")
    r.add_argument("--min-match", type=float, default=0.5)
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("ats-test", help="score a resume against real ATS parsers")
    a.add_argument("--pdf", required=True)
    a.add_argument("--lever-url", help="a jobs.lever.co/<co>/<id>/apply URL for a live parse test")
    a.set_defaults(func=cmd_ats_test)

    s = sub.add_parser("stats", help="summarize the tracker")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
