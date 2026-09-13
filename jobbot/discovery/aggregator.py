"""LinkedIn / Indeed / Glassdoor discovery via JobSpy, with ATS link recovery.

The ATS APIs only reach companies whose board slug you already know. That misses
exactly the population worth applying to: small and early-stage companies that
post on LinkedIn and put the real application link in the post body.

So this layer does two things:

  1. Scrape the aggregators for postings matching the search.
  2. Recover the underlying ATS URL -- from JobSpy's `job_url_direct`, and
     failing that by scanning the description text for a Greenhouse, Lever,
     Ashby, Workday, Workable or SmartRecruiters link.

Recovering that link matters beyond convenience. Applying on the company's own
ATS rather than through the aggregator is the higher-converting channel by a
wide margin (roughly 34% of hires from 24% of applications, versus 23% of hires
from 50% via job boards), and it avoids automating LinkedIn's own apply flow --
the one platform with documented account bans for exactly that.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

import structlog

from jobbot.ats.detect import ATS, detect
from jobbot.discovery.sources import JobPost, _strip_html

log = structlog.get_logger(__name__)

# Apply links people paste into a post body or that sit behind "Apply on company site".
_ATS_URL = re.compile(
    r"https?://[^\s\"'<>)\]]*?("
    r"(?:job-)?boards\.greenhouse\.io/[^\s\"'<>)\]]+"
    r"|jobs\.lever\.co/[^\s\"'<>)\]]+"
    r"|jobs\.ashbyhq\.com/[^\s\"'<>)\]]+"
    r"|[a-z0-9-]+\.(?:wd\d+\.)?myworkdayjobs\.com/[^\s\"'<>)\]]+"
    r"|[a-z0-9-]+\.myworkdaysite\.com/[^\s\"'<>)\]]+"
    r"|apply\.workable\.com/[^\s\"'<>)\]]+"
    r"|jobs\.smartrecruiters\.com/[^\s\"'<>)\]]+"
    r"|[a-z0-9-]+\.breezy\.hr/[^\s\"'<>)\]]+"
    r"|[a-z0-9-]+\.bamboohr\.com/careers/[^\s\"'<>)\]]+"
    r"|careers-[a-z0-9-]+\.icims\.com/[^\s\"'<>)\]]+"
    r"|[^\s\"'<>)\]]*[?&]gh_jid=\d+"
    r")",
    re.I,
)

DEFAULT_SITES = ["linkedin", "indeed", "google", "zip_recruiter"]


def extract_ats_url(*texts: str) -> str | None:
    """First real ATS application URL found in any of `texts`."""
    for t in texts:
        if not t:
            continue
        m = _ATS_URL.search(t)
        if m:
            url = m.group(0).rstrip(".,);:'\"")
            if detect(url).ats is not ATS.UNKNOWN:
                return url
    return None


def _to_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    try:
        import pandas as pd
        if pd.isna(v):
            return None
    except Exception:  # noqa: BLE001
        pass
    try:
        dt = datetime.fromisoformat(str(v))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _num(v: Any) -> int | None:
    try:
        import pandas as pd
        if v is None or pd.isna(v):
            return None
    except Exception:  # noqa: BLE001
        if v is None:
            return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def scrape(
    search_term: str,
    *,
    location: str = "San Francisco, CA",
    sites: Iterable[str] | None = None,
    results_wanted: int = 60,
    hours_old: int | None = 336,
    is_remote: bool = False,
    country_indeed: str = "USA",
    require_ats_link: bool = False,
) -> list[JobPost]:
    """Scrape the aggregators and return postings, ATS-resolved where possible."""
    from jobspy import scrape_jobs

    sites = list(sites or DEFAULT_SITES)
    try:
        df = scrape_jobs(
            site_name=sites,
            search_term=search_term,
            google_search_term=f"{search_term} jobs near {location}",
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            is_remote=is_remote,
            country_indeed=country_indeed,
            linkedin_fetch_description=True,   # needed to recover apply links
            description_format="markdown",
            verbose=0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("aggregator.scrape_failed", term=search_term, error=str(exc)[:200])
        return []

    if df is None or len(df) == 0:
        log.info("aggregator.empty", term=search_term, sites=sites)
        return []

    out: list[JobPost] = []
    recovered = 0
    for _, row in df.iterrows():
        r = row.to_dict()
        desc = _strip_html(str(r.get("description") or ""))
        listing_url = str(r.get("job_url") or "")
        direct = str(r.get("job_url_direct") or "")

        ats_url = extract_ats_url(direct, desc, listing_url)
        apply_url = ats_url or direct or listing_url
        det = detect(apply_url)
        if det.ats is ATS.LINKEDIN and not ats_url:
            # A LinkedIn listing URL is not an application endpoint; leave it
            # unresolved so board enrichment gets a chance at it.
            det = detect("")
        if ats_url:
            recovered += 1

        if require_ats_link and det.ats in (ATS.UNKNOWN, ATS.LINKEDIN):
            continue

        site = str(r.get("site") or "aggregator")
        native = det.native_id or str(r.get("id") or listing_url)[-40:]
        out.append(JobPost(
            ats=det.ats,
            native_id=native,
            company=str(r.get("company") or "").strip(),
            title=str(r.get("title") or "").strip(),
            url=apply_url,
            location=str(r.get("location") or "").strip(),
            remote=bool(r.get("is_remote")) if r.get("is_remote") is not None else False,
            description=desc,
            department="",
            posted_at=_to_dt(r.get("date_posted")),
            salary_min=_num(r.get("min_amount")),
            salary_max=_num(r.get("max_amount")),
            raw={"source_site": site, "listing_url": listing_url,
                 "job_url_direct": direct, "recovered_ats_url": ats_url},
        ))

    log.info("aggregator.scraped", term=search_term, sites=sites,
             found=len(out), ats_links_recovered=recovered)
    return out


# -- company -> ATS board resolution --------------------------------------

_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|ltd|corp|corporation|co|company|technologies|technology|"
    r"labs|systems|software|group|holdings|the)\b", re.I)


def company_slugs(company: str) -> list[str]:
    """Plausible board slugs for a company name, most likely first."""
    base = _SUFFIXES.sub(" ", company or "").strip()
    base = re.sub(r"[^\w\s-]", " ", base)
    words = [w for w in re.split(r"[\s_-]+", base) if w]
    if not words:
        return []
    joined = "".join(words).lower()
    hyphen = "-".join(words).lower()
    first = words[0].lower()
    out = [joined, hyphen, first]
    if len(words) > 1:
        out.append("".join(words[:2]).lower())
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen and len(s) > 1:
            seen.add(s)
            uniq.append(s)
    return uniq[:4]


async def resolve_company_board(company: str, *, timeout: float = 12.0) -> list[JobPost]:
    """Find a company's real ATS board by trying candidate slugs.

    LinkedIn listings rarely carry the underlying apply link, but the company
    name is right there and every major ATS serves its board unauthenticated.
    Resolving the board turns an unapplicable LinkedIn row into a real, directly
    applicable posting -- on the higher-converting channel.
    """
    from jobbot.discovery.sources import Discovery

    d = Discovery(timeout=timeout)
    for slug in company_slugs(company):
        for name, fn in (("greenhouse", d.greenhouse), ("lever", d.lever),
                         ("ashby", d.ashby)):
            try:
                jobs = await fn(slug)
            except Exception:  # noqa: BLE001
                continue
            if jobs:
                log.info("aggregator.board_resolved", company=company,
                         ats=name, slug=slug, jobs=len(jobs))
                return jobs
    return []


async def enrich_with_boards(
    posts: list[JobPost], *, max_companies: int = 25, concurrency: int = 5,
) -> list[JobPost]:
    """Replace aggregator rows with real ATS postings where a board is found.

    Matches by title similarity so we apply to the same role we discovered,
    not merely to some role at the same company.
    """
    import asyncio

    from jobbot.forms.matching import normalize

    unresolved = [p for p in posts if p.ats in (ATS.UNKNOWN, ATS.LINKEDIN)]
    companies: list[str] = []
    for p in unresolved:
        if p.company and p.company not in companies:
            companies.append(p.company)
    companies = companies[:max_companies]

    sem = asyncio.Semaphore(concurrency)

    async def one(c: str) -> tuple[str, list[JobPost]]:
        async with sem:
            return c, await resolve_company_board(c)

    boards = dict(await asyncio.gather(*(one(c) for c in companies)))

    resolved: list[JobPost] = []
    replaced = 0
    for p in posts:
        if p.ats not in (ATS.UNKNOWN, ATS.LINKEDIN):
            resolved.append(p)
            continue
        cand = boards.get(p.company) or []
        want = set(normalize(p.title).split())
        best, best_overlap = None, 0.0
        for j in cand:
            got = set(normalize(j.title).split())
            if not got:
                continue
            overlap = len(want & got) / max(1, len(want | got))
            if overlap > best_overlap:
                best, best_overlap = j, overlap
        if best is not None and best_overlap >= 0.45:
            best.raw["discovered_via"] = "linkedin"
            best.raw["linkedin_url"] = p.url
            resolved.append(best)
            replaced += 1
        else:
            resolved.append(p)

    log.info("aggregator.enriched", unresolved=len(unresolved),
             companies_tried=len(companies),
             boards_found=sum(1 for v in boards.values() if v), replaced=replaced)
    return resolved
