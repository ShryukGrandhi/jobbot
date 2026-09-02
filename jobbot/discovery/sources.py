"""Job discovery via each ATS's own public API.

Every endpoint below serves JSON to an unauthenticated GET/POST. That is a
deliberate choice over browser scraping: it is faster, it does not touch bot
protection, it returns structured data instead of parsed markup, and it does not
degrade when a marketing team reskins the careers page.

The archived JobFunnel's post-mortem is the argument for this design in the
author's own words -- aggregator scraping became "too slow, fragile, and
operationally complex" once boards moved behind real anti-automation. The ATS
APIs did not move.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Iterable

import httpx
import structlog

from jobbot.ats.detect import ATS

log = structlog.get_logger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

# 403/406 mean a source started blocking us. They are deliberately NOT retried:
# a newly-blocking ATS must fail loudly rather than decay into a silent slow
# path that returns zero jobs and looks like "no results today".
RETRYABLE = {408, 429, 500, 502, 503, 504}
HARD_BLOCK = {401, 403, 406}


class SourceBlocked(RuntimeError):
    """The ATS is refusing us. Surfaces instead of silently returning []."""


@dataclass
class JobPost:
    ats: ATS
    native_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    description: str = ""
    department: str = ""
    posted_at: datetime | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)   # never lose ATS-specific fields

    @property
    def job_id(self) -> str:
        return f"{self.ats.value}:{self.native_id}"

    @property
    def age_days(self) -> float | None:
        if not self.posted_at:
            return None
        return (datetime.now(timezone.utc) - self.posted_at).total_seconds() / 86400


def _strip_html(s: str) -> str:
    # Greenhouse double-encodes: unescape entities first, THEN strip tags.
    s = unescape(s or "")
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", unescape(s)).strip()


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_remote(text: str) -> bool:
    return bool(re.search(r"\bremote\b|\bwork from home\b|\banywhere\b", text or "", re.I))


def _ashby_salary(comp: dict[str, Any]) -> tuple[int | None, int | None]:
    """Pull a base-salary band out of Ashby's compensation payload.

    Ashby exposes both a structured tier list and a rendered summary string
    ("$150K - $200K + Equity"). Prefer the structured form; fall back to
    scraping the summary, expanding a trailing K.
    """
    tiers = comp.get("compensationTiers") or []
    for t in tiers:
        for c in t.get("components") or []:
            if str(c.get("summaryType", "")).lower() in ("salary", "base"):
                lo, hi = c.get("minValue"), c.get("maxValue")
                if lo or hi:
                    return (int(lo) if lo else None, int(hi) if hi else None)

    summary = comp.get("compensationTierSummary") or ""
    nums: list[int] = []
    for raw, suffix in re.findall(r"\$\s*([\d,.]+)\s*([KkMm]?)", summary):
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix.lower() == "k":
            val *= 1_000
        elif suffix.lower() == "m":
            val *= 1_000_000
        nums.append(int(val))
    if len(nums) >= 2:
        return min(nums), max(nums)
    if len(nums) == 1:
        return nums[0], None
    return None, None


class Discovery:
    def __init__(self, timeout: float = 30.0, concurrency: int = 6) -> None:
        self.timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)

    async def _get(self, client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
        async with self._sem:
            r = await client.get(url, timeout=self.timeout, **kw)
        if r.status_code in HARD_BLOCK:
            raise SourceBlocked(f"{r.status_code} from {url}")
        r.raise_for_status()
        return r.json()

    # -- Greenhouse -------------------------------------------------------

    async def greenhouse(self, slug: str) -> list[JobPost]:
        """The most permissive ATS API: no auth, no practical rate limit."""
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as c:
            data = await self._get(c, url)
        out = []
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            out.append(JobPost(
                ats=ATS.GREENHOUSE, native_id=str(j.get("id")), company=slug,
                title=j.get("title", ""), url=j.get("absolute_url", ""),
                location=loc, remote=_is_remote(f"{loc} {j.get('title','')}"),
                description=_strip_html(j.get("content", "")),
                department=", ".join(d.get("name", "") for d in j.get("departments", [])),
                posted_at=_parse_dt(j.get("updated_at") or j.get("first_published")),
                raw=j,
            ))
        return out

    # -- Lever ------------------------------------------------------------

    async def lever(self, slug: str) -> list[JobPost]:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as c:
            data = await self._get(c, url)
        out = []
        for j in data:
            cats = j.get("categories") or {}
            loc = cats.get("location", "") or ""
            out.append(JobPost(
                ats=ATS.LEVER, native_id=str(j.get("id")), company=slug,
                title=j.get("text", ""), url=j.get("hostedUrl", ""),
                location=loc,
                remote=_is_remote(f"{loc} {cats.get('commitment','')} {j.get('workplaceType','')}"),
                description=_strip_html(j.get("descriptionPlain") or j.get("description", "")),
                department=cats.get("team", "") or "",
                posted_at=_parse_dt(j.get("createdAt")),
                raw=j,
            ))
        return out

    # -- Ashby ------------------------------------------------------------

    async def ashby(self, slug: str) -> list[JobPost]:
        url = (f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
               "?includeCompensation=true")
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as c:
            data = await self._get(c, url)
        out = []
        for j in data.get("jobs", []):
            lo, hi = _ashby_salary(j.get("compensation") or {})
            out.append(JobPost(
                ats=ATS.ASHBY, native_id=str(j.get("id")), company=slug,
                title=j.get("title", ""), url=j.get("jobUrl", ""),
                location=j.get("location", "") or "",
                remote=bool(j.get("isRemote")) or _is_remote(j.get("location", "")),
                description=_strip_html(j.get("descriptionPlain") or j.get("descriptionHtml", "")),
                department=j.get("department", "") or j.get("team", "") or "",
                posted_at=_parse_dt(j.get("publishedAt") or j.get("updatedAt")),
                salary_min=lo, salary_max=hi,
                raw=j,
            ))
        return out

    # -- Workday ----------------------------------------------------------

    async def workday(self, tenant: str, site: str, pod: str = "wd1",
                      search: str = "", max_jobs: int = 200) -> list[JobPost]:
        """Workday's own posting search.

        Three hard constraints, all load-bearing:
          * `limit` is capped at 20 server-side; anything higher returns 400.
          * capped tenants report total == exactly 2000 and pagination past
            offset 2000 wraps back to page 1 -- so we stop there rather than
            loop forever re-ingesting page one.
          * the response carries facet counts, so a deeper crawl can subdivide
            by jobFamilyGroup instead of blindly paging.
        """
        base = f"https://{tenant}.{pod}.myworkdayjobs.com"
        url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        out: list[JobPost] = []
        offset = 0
        PAGE, CAP = 20, 2000
        async with httpx.AsyncClient(headers={"User-Agent": UA, "Content-Type": "application/json"}) as c:
            while offset < min(max_jobs, CAP):
                async with self._sem:
                    r = await c.post(url, json={"appliedFacets": {}, "limit": PAGE,
                                                "offset": offset, "searchText": search},
                                     timeout=self.timeout)
                if r.status_code in HARD_BLOCK:
                    raise SourceBlocked(f"{r.status_code} from {url}")
                r.raise_for_status()
                data = r.json()
                posts = data.get("jobPostings", [])
                if not posts:
                    break
                for j in posts:
                    ext = j.get("externalPath", "")
                    out.append(JobPost(
                        ats=ATS.WORKDAY, native_id=(ext.rsplit("_", 1)[-1] or ext),
                        company=tenant, title=j.get("title", ""),
                        url=f"{base}{ext}", location=j.get("locationsText", "") or "",
                        remote=_is_remote(j.get("locationsText", "")),
                        description="",  # detail requires a second fetch
                        posted_at=None, raw=j,
                    ))
                total = data.get("total", 0)
                offset += PAGE
                # A capped tenant reports total == exactly CAP and wraps to page
                # one beyond that offset, so bound by CAP -- but keep paging up
                # to it rather than bailing after the first page.
                if offset >= min(total or CAP, CAP):
                    break
        return out

    # -- SmartRecruiters --------------------------------------------------

    async def smartrecruiters(self, slug: str) -> list[JobPost]:
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as c:
            data = await self._get(c, url)
        out = []
        for j in data.get("content", []):
            loc = j.get("location") or {}
            loctxt = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                           loc.get("country")) if x)
            out.append(JobPost(
                ats=ATS.SMARTRECRUITERS, native_id=str(j.get("id")), company=slug,
                title=j.get("name", ""),
                url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                location=loctxt, remote=bool(loc.get("remote")),
                department=(j.get("department") or {}).get("label", ""),
                posted_at=_parse_dt(j.get("releasedDate")), raw=j,
            ))
        return out

    # -- Workable ---------------------------------------------------------

    async def workable(self, slug: str) -> list[JobPost]:
        url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
        async with httpx.AsyncClient(headers={"User-Agent": UA}) as c:
            async with self._sem:
                r = await c.post(url, json={}, timeout=self.timeout)
            if r.status_code in HARD_BLOCK:
                raise SourceBlocked(f"{r.status_code} from {url}")
            r.raise_for_status()
            data = r.json()
        out = []
        for j in data.get("results", []):
            out.append(JobPost(
                ats=ATS.WORKABLE, native_id=str(j.get("shortcode") or j.get("id")),
                company=slug, title=j.get("title", ""),
                url=f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
                location=(j.get("location") or {}).get("city", "") or "",
                remote=bool(j.get("remote")),
                department=j.get("department", "") or "",
                posted_at=_parse_dt(j.get("published_on")), raw=j,
            ))
        return out


# -- ghost-job scoring ----------------------------------------------------

def ghost_score(post: JobPost, company_posts: Iterable[JobPost] | None = None) -> float:
    """Heuristic 0-1 that a posting will never hire anyone.

    Worth its own pass: roughly 18-22% of postings are ghost jobs and ~30% of
    requisitions close with nobody hired. That wastes more applications than any
    resume variable recovers, and it is completely invisible to the applicant.

    Signals, in rough order of reliability:
      * age -- a req is typically live 8-10 weeks; far beyond that with no
        change is the strongest single tell.
      * evergreen phrasing -- "always hiring", "talent pool", "future opening".
      * duplicate titles open concurrently at the same company.
    """
    score = 0.0
    age = post.age_days
    if age is not None:
        if age > 120:
            score += 0.45
        elif age > 75:
            score += 0.25
        elif age > 45:
            score += 0.10

    blob = f"{post.title} {post.description[:2000]}".lower()
    for pat, w in (
        (r"\balways (be )?(hiring|accepting)\b", 0.35),
        (r"\btalent (pool|community|network)\b", 0.35),
        (r"\bfuture (opening|opportunit)", 0.30),
        (r"\bgeneral (application|interest)\b", 0.30),
        (r"\bexpression of interest\b", 0.25),
        (r"\bpipeline (role|req)\b", 0.25),
        (r"\bevergreen\b", 0.30),
    ):
        if re.search(pat, blob):
            score += w

    if company_posts:
        same = sum(1 for p in company_posts
                   if p.title.strip().lower() == post.title.strip().lower())
        if same > 2:
            score += 0.15

    return round(min(1.0, score), 3)
