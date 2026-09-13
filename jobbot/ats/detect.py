"""Identify which ATS a job URL belongs to.

Matching is done on query-string fingerprints as well as hostnames. Many
employers embed Greenhouse or Lever on their own careers domain, where the
hostname tells you nothing but `?gh_jid=` or `?LeverAppId=` is definitive. A
hostname-only matcher silently misses the entire self-hosted long tail.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


class ATS(str, enum.Enum):
    WORKDAY = "workday"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    ICIMS = "icims"
    TALEO = "taleo"
    SUCCESSFACTORS = "successfactors"
    SMARTRECRUITERS = "smartrecruiters"
    WORKABLE = "workable"
    JOBVITE = "jobvite"
    BAMBOOHR = "bamboohr"
    BREEZY = "breezy"
    RIPPLING = "rippling"
    AVATURE = "avature"
    DOVER = "dover"
    ORACLE = "oracle"
    LINKEDIN = "linkedin"
    UNKNOWN = "unknown"


# Ordered: the first match wins, so put the specific before the general.
_HOST_PATTERNS: list[tuple[ATS, re.Pattern[str]]] = [
    # Workday shards tenants across wd1/wd2/wd3/wd5/wd10/wd12/wd103 and also
    # serves myworkdaysite.com. Pinning one pod is the documented root cause of
    # "Workday support" that silently covers a minority of real postings.
    (ATS.WORKDAY, re.compile(r"(^|\.)(wd\d+\.)?myworkdayjobs\.com$", re.I)),
    (ATS.WORKDAY, re.compile(r"(^|\.)myworkdaysite\.com$", re.I)),
    (ATS.GREENHOUSE, re.compile(r"(^|\.)(job-)?boards\.greenhouse\.io$", re.I)),
    (ATS.GREENHOUSE, re.compile(r"(^|\.)greenhouse\.io$", re.I)),
    (ATS.LEVER, re.compile(r"(^|\.)jobs\.lever\.co$", re.I)),
    (ATS.LEVER, re.compile(r"(^|\.)lever\.co$", re.I)),
    (ATS.ASHBY, re.compile(r"(^|\.)jobs\.ashbyhq\.com$", re.I)),
    (ATS.ASHBY, re.compile(r"(^|\.)ashbyhq\.com$", re.I)),
    (ATS.ICIMS, re.compile(r"(^|\.)icims\.com$", re.I)),
    (ATS.ICIMS, re.compile(r"(^|\.)jibeapply\.com$", re.I)),
    (ATS.TALEO, re.compile(r"(^|\.)taleo\.net$", re.I)),
    # Oracle Cloud HCM. Nine American Express internships were filed
    # "no ATS apply URL resolved" because this host was unrecognised.
    (ATS.ORACLE, re.compile(r"(^|\.)oraclecloud\.com$", re.I)),
    (ATS.ORACLE, re.compile(r"(^|\.)oracle\.com$", re.I)),
    (ATS.SUCCESSFACTORS, re.compile(r"(^|\.)(successfactors\.(com|eu)|sapsf\.(com|eu)|ns2cloud\.com)$", re.I)),
    (ATS.SMARTRECRUITERS, re.compile(r"(^|\.)smartrecruiters\.com$", re.I)),
    (ATS.WORKABLE, re.compile(r"(^|\.)workable\.com$", re.I)),
    (ATS.JOBVITE, re.compile(r"(^|\.)jobvite\.com$", re.I)),
    (ATS.BAMBOOHR, re.compile(r"(^|\.)bamboohr\.(com|co\.uk)$", re.I)),
    (ATS.BREEZY, re.compile(r"(^|\.)breezy\.hr$", re.I)),
    (ATS.RIPPLING, re.compile(r"(^|\.)(ats\.)?rippling(ats)?\.com$", re.I)),
    (ATS.AVATURE, re.compile(r"(^|\.)avature\.net$", re.I)),
    (ATS.DOVER, re.compile(r"(^|\.)dover\.com$", re.I)),
    (ATS.LINKEDIN, re.compile(r"(^|\.)linkedin\.com$", re.I)),
]

# Query params that identify an embedded board on an arbitrary host.
_QUERY_FINGERPRINTS: list[tuple[ATS, str]] = [
    (ATS.GREENHOUSE, "gh_jid"),
    (ATS.GREENHOUSE, "gh_src"),
    (ATS.LEVER, "leverappid"),
    (ATS.ASHBY, "ashby_jid"),
]

# Markup that betrays an embedded board when neither host nor query does.
_DOM_FINGERPRINTS: list[tuple[ATS, re.Pattern[str]]] = [
    (ATS.GREENHOUSE, re.compile(r"greenhouse\.io/embed|id=[\"']grnhse_app[\"']", re.I)),
    (ATS.LEVER, re.compile(r"jobs\.lever\.co|lever-application", re.I)),
    (ATS.ASHBY, re.compile(r"ashbyhq\.com|_ashby_embed", re.I)),
    (ATS.WORKDAY, re.compile(r"data-automation-id|myworkdayjobs", re.I)),
    (ATS.SMARTRECRUITERS, re.compile(r"smartrecruiters\.com/embed", re.I)),
]


@dataclass
class Detection:
    ats: ATS
    tenant: str | None = None      # company slug / Workday tenant
    site: str | None = None        # Workday career-site path segment
    native_id: str | None = None   # the ATS's own posting id
    confidence: float = 1.0

    @property
    def job_id(self) -> str:
        """Stable global key. Split on the FIRST colon when parsing back."""
        return f"{self.ats.value}:{self.native_id or self.tenant or 'unknown'}"


def detect(url: str, html: str | None = None) -> Detection:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    qs = {k.lower(): v for k, v in parse_qs(p.query).items()}

    for ats, param in _QUERY_FINGERPRINTS:
        if param in qs:
            return Detection(ats=ats, native_id=qs[param][0], confidence=0.95)

    for ats, pat in _HOST_PATTERNS:
        if pat.search(host):
            return _enrich(ats, p, host)

    if html:
        for ats, pat in _DOM_FINGERPRINTS:
            if pat.search(html):
                return Detection(ats=ats, confidence=0.6)

    return Detection(ats=ATS.UNKNOWN, confidence=0.0)


def _enrich(ats: ATS, p, host: str) -> Detection:
    """Pull tenant/site/id out of the path for the ATSes we can apply to."""
    parts = [s for s in p.path.split("/") if s]

    if ats is ATS.WORKDAY:
        # https://{tenant}.wd5.myworkdayjobs.com/{lang}/{site}/job/{loc}/{slug}_{id}
        tenant = host.split(".")[0]
        site = None
        for i, seg in enumerate(parts):
            if seg.lower().startswith("en-") or len(seg) == 2:
                site = parts[i + 1] if i + 1 < len(parts) else None
                break
        if site is None and parts:
            site = parts[0]
        native = parts[-1] if parts else None
        return Detection(ats, tenant=tenant, site=site, native_id=native)

    if ats is ATS.GREENHOUSE:
        # boards.greenhouse.io/{slug}/jobs/{id}
        tenant = parts[0] if parts else None
        native = next((s for s in reversed(parts) if s.isdigit()), None)
        return Detection(ats, tenant=tenant, native_id=native)

    if ats is ATS.LEVER:
        # jobs.lever.co/{slug}/{uuid}
        tenant = parts[0] if parts else None
        native = parts[1] if len(parts) > 1 else None
        return Detection(ats, tenant=tenant, native_id=native)

    if ats is ATS.ASHBY:
        # jobs.ashbyhq.com/{slug}/{uuid}
        tenant = parts[0] if parts else None
        native = parts[1] if len(parts) > 1 else None
        return Detection(ats, tenant=tenant, native_id=native)

    if ats is ATS.SMARTRECRUITERS:
        tenant = parts[0] if parts else None
        return Detection(ats, tenant=tenant, native_id=parts[-1] if parts else None)

    if ats is ATS.WORKABLE:
        # apply.workable.com/{company}/j/{id}
        tenant = parts[0] if parts else None
        native = parts[-1] if parts else None
        return Detection(ats, tenant=tenant, native_id=native)

    return Detection(ats, tenant=host.split(".")[0], native_id=parts[-1] if parts else None)


# Which ATSes force account creation before the form is reachable. This is the
# dominant failure mode in the wild: in one 1,503-job field report, 470 of 589
# apply failures (80%) were "Workday login required" -- not selector rot, not
# captchas. Everything else is a rounding error next to it.
REQUIRES_ACCOUNT = {ATS.WORKDAY, ATS.TALEO, ATS.SUCCESSFACTORS, ATS.ICIMS, ATS.AVATURE}
