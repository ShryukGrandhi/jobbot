"""Score a tailored resume against one job description.

The scoring model -- weighting, whole-word keyword matching, and the
keyword/skills/sections split -- is adapted from Resume-Matcher
(github.com/srbhr/Resume-Matcher, Apache-2.0), reimplemented against our own
data structures and extended with a parseability check.

An honest note on what this number is and is not. No ATS vendor documents
scoring or auto-rejecting a resume on keyword density; the widely repeated "75%
of resumes are rejected by ATS" traces to a 2012 sales pitch by a company that
folded in 2013. Real auto-rejection fires on structured screening ANSWERS, not
on parsed prose.

So this score is not a gate anyone actually applies. It is a proxy for something
real and duller: whether a human skimming for 30 seconds will see the
vocabulary of their own job posting, and whether the document parses cleanly.
Both genuinely matter. Treat it as a drafting aid, not a verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

WEIGHTS = {
    "keyword_match": 0.34,
    "skills_coverage": 0.20,
    "section_completeness": 0.10,
    "parseability": 0.14,
    "experience_clarity": 0.14,   # dates parseable, bullets quantified
    "impact_density": 0.08,       # how much of the page carries a number
}

SECTIONS = {
    "summary": ("summary", "objective", "profile", "about"),
    "experience": ("experience", "work history", "employment"),
    "education": ("education", "academic", "degree", "university"),
    "skills": ("skills", "technologies", "competencies", "technical"),
}

# Words that look like requirements but carry no signal.
_STOP = {
    "the", "and", "for", "with", "you", "your", "our", "will", "are", "have",
    "this", "that", "from", "they", "their", "who", "what", "when", "how",
    "work", "team", "role", "job", "years", "year", "experience", "strong",
    "ability", "including", "such", "using", "must", "should", "would", "can",
    "help", "build", "building", "working", "across", "into", "about", "more",
    "well", "also", "than", "them", "these", "those", "been", "being", "some",
    "like", "make", "want", "need", "know", "good", "great", "new", "use",
}

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#./_-]{1,}")

# A phrase is only kept if at least one token looks technical. Keeps
# "distributed systems" and drops "excellent communication".
_TECH_HINT = {
    "api", "apis", "sql", "postgres", "postgresql", "python", "typescript",
    "javascript", "react", "next.js", "node", "docker", "kubernetes", "aws",
    "gcp", "azure", "etl", "pipeline", "pipelines", "backend", "frontend",
    "full-stack", "fullstack", "stack", "infrastructure", "distributed",
    "systems", "data", "ml", "ai", "llm", "llms", "rag", "model", "models",
    "inference", "training", "vector", "embedding", "embeddings", "graphql",
    "rest", "grpc", "redis", "kafka", "spark", "airflow", "terraform", "linux",
    "microservices", "serverless", "streaming", "realtime", "real-time",
    "latency", "throughput", "scaling", "observability", "testing", "ci/cd",
    "agents", "agentic", "fine-tuning", "evaluation", "prompt", "schema",
    "database", "databases", "queue", "queues", "cache", "caching", "web",
    "mobile", "ios", "android", "swift", "kotlin", "rust", "golang", "go",
    "java", "c++", "security", "cloud", "devops", "platform", "product",
}


def _words(text: str) -> list[str]:
    return [w.lower() for w in _TOKEN.findall(text or "")]


def whole_word(term: str, text_lower: str) -> bool:
    """Whole-word containment; avoids 'go' matching inside 'google'."""
    esc = re.escape(term.strip().lower())
    if not esc:
        return False
    return bool(re.search(rf"(?<!\w){esc}(?!\w)", text_lower))


def resume_text(tailored: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, str):
            parts.append(o)
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)

    walk(tailored)
    return " ".join(parts)


def jd_keywords(job_description: str, *, top_n: int = 40) -> list[str]:
    """Salient terms from the JD, by frequency, with junk removed."""
    counts: dict[str, int] = {}
    for w in _words(job_description):
        w = w.strip(".,;:!?-_/")
        if len(w) < 3 or w in _STOP or w.isdigit():
            continue
        counts[w] = counts.get(w, 0) + 1

    # Multi-word technical phrases carry more signal than their parts, but only
    # if every token is meaningful -- otherwise we "require" phrases like
    # "hiring a" and "is required", which are noise dressed up as signal.
    # Split on sentence and clause boundaries first: a phrase that spans a
    # period ("typescript. design postgresql") is an artifact, not a requirement.
    clauses = re.split(r"[.;:!?\n\r\u2022()\[\]]+|,\s", job_description or "")
    phrases: list[str] = []
    for clause in clauses:
        phrases += re.findall(
            r"\b([A-Za-z][\w+#-]*(?:\s[A-Za-z][\w+#-]*){1,2})\b", clause)
    for ph in phrases:
        ph = ph.strip().lower().rstrip(".,;:")
        toks = ph.split()
        if not (6 <= len(ph) <= 34) or len(toks) < 2:
            continue
        if any(t in _STOP or len(t) < 3 or t.isdigit() for t in toks):
            continue
        if not any(t in _TECH_HINT for t in toks):
            continue
        counts[ph] = counts.get(ph, 0) + 2

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[:top_n]]


_DATE = re.compile(
    r"(19|20)\d{2}|present|current|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", re.I)
_NUMBERISH = re.compile(r"\d[\d,.]*\s*(%|x\b|k\b|m\b|b\b|ms\b|s\b|/day|/s|rps|qps)?", re.I)
_WEAK_VERB = re.compile(
    r"^\s*(responsible for|worked on|helped|assisted|participated|involved in|"
    r"tasked with|duties included)", re.I)


def _experience_clarity(tailored: dict[str, Any]) -> tuple[float, list[str]]:
    """Dates parseable, bullets concrete, no filler openings."""
    roles = tailored.get("experience") or []
    if not roles:
        return 0.0, ["No experience section."]
    notes: list[str] = []
    dated = sum(1 for r in roles if _DATE.search(str(r.get("dates", ""))))
    titled = sum(1 for r in roles if r.get("title") and r.get("company"))

    bullets = [b for r in roles for b in (r.get("bullets") or [])]
    if not bullets:
        return 30.0, ["Roles have no bullets."]
    quantified = sum(1 for b in bullets if _NUMBERISH.search(b))
    weak = [b for b in bullets if _WEAK_VERB.search(b)]

    score = (
        40 * (dated / len(roles))
        + 20 * (titled / len(roles))
        + 40 * min(1.0, quantified / max(1, len(bullets)) / 0.6)
    )
    score -= 8 * len(weak)
    if dated < len(roles):
        notes.append(f"{len(roles) - dated} role(s) missing parseable dates - "
                     "ATS date extraction depends on these.")
    if quantified / len(bullets) < 0.5:
        notes.append(f"Only {quantified}/{len(bullets)} bullets carry a number.")
    for b in weak[:2]:
        notes.append(f"Filler opening: {b[:60]!r}")
    return max(0.0, min(100.0, score)), notes


def _impact_density(tailored: dict[str, Any]) -> tuple[float, list[str]]:
    """Share of bullets that state a measurable outcome."""
    bullets = [b for r in (tailored.get("experience") or []) for b in (r.get("bullets") or [])]
    bullets += [b for p_ in (tailored.get("projects") or []) for b in (p_.get("bullets") or [])]
    if not bullets:
        return 0.0, []
    hits = sum(1 for b in bullets if _NUMBERISH.search(b))
    ratio = hits / len(bullets)
    notes = ([] if ratio >= 0.5 else
             [f"{hits}/{len(bullets)} bullets quantified; aim for half or more."])
    return min(100.0, ratio / 0.6 * 100), notes


@dataclass
class ATSScore:
    overall: float = 0.0
    keyword_match: float = 0.0
    skills_coverage: float = 0.0
    section_completeness: float = 0.0
    parseability: float = 0.0
    experience_clarity: float = 0.0
    impact_density: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    injectable: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (f"ATS {self.overall:.0f}/100  (keywords {self.keyword_match:.0f}, "
                f"skills {self.skills_coverage:.0f}, sections {self.section_completeness:.0f}, "
                f"parse {self.parseability:.0f}, clarity {self.experience_clarity:.0f}, "
                f"impact {self.impact_density:.0f})")


def score(
    tailored: dict[str, Any],
    job_description: str,
    *,
    profile_skills: dict[str, list[str]] | None = None,
    pdf_text: str | None = None,
) -> ATSScore:
    """Score a tailored resume against one JD."""
    text = resume_text(tailored)
    low = text.lower()

    keys = jd_keywords(job_description)
    matched = [k for k in keys if whole_word(k, low)]
    missing = [k for k in keys if k not in matched]
    kw = (len(matched) / len(keys) * 100) if keys else 0.0

    # Skills the JD names that the candidate genuinely has but this draft omitted.
    injectable: list[str] = []
    if profile_skills:
        have = {s.lower() for items in profile_skills.values() for s in items}
        jd_low = (job_description or "").lower()
        for s in have:
            if whole_word(s, jd_low) and not whole_word(s, low):
                injectable.append(s)

    resume_skills = {s.lower()
                     for items in (tailored.get("skills") or {}).values()
                     for s in (items if isinstance(items, list) else [])}
    jd_low = (job_description or "").lower()
    jd_skill_terms = [s for s in resume_skills if whole_word(s, jd_low)]
    all_jd_skills = set(jd_skill_terms) | set(injectable)
    skills_cov = (len(jd_skill_terms) / len(all_jd_skills) * 100) if all_jd_skills else 100.0

    found = sum(1 for name in SECTIONS
                if (name == "summary" and tailored.get("summary"))
                or (name == "experience" and tailored.get("experience"))
                or (name == "education" and tailored.get("education"))
                or (name == "skills" and tailored.get("skills")))
    sections = found / len(SECTIONS) * 100

    # Parseability, measured against the real PDF text layer when available.
    parse = 100.0
    notes: list[str] = []
    if pdf_text is not None:
        if len(pdf_text.strip()) < 200:
            parse -= 60
            notes.append("PDF text layer is nearly empty - the resume may be rendering as an image.")
        ident = tailored.get("identity", {})
        for label, val in (("name", ident.get("name")), ("email", ident.get("email"))):
            if val and val.lower() not in pdf_text.lower():
                parse -= 20
                notes.append(f"{label} does not survive text extraction.")
        if re.search(r"\t{2,}| {6,}\S+ {6,}", pdf_text):
            parse -= 10
            notes.append("Column-like spacing detected; single-column parses more reliably.")
    parse = max(0.0, parse)

    clarity, clarity_notes = _experience_clarity(tailored)
    impact, impact_notes = _impact_density(tailored)
    notes += clarity_notes + impact_notes

    overall = (WEIGHTS["keyword_match"] * kw
               + WEIGHTS["skills_coverage"] * skills_cov
               + WEIGHTS["section_completeness"] * sections
               + WEIGHTS["parseability"] * parse
               + WEIGHTS["experience_clarity"] * clarity
               + WEIGHTS["impact_density"] * impact)

    recs = list(notes)
    if injectable:
        recs.append("Real skills the JD asks for that this draft omits: "
                    + ", ".join(sorted(injectable)[:8]) + ".")
    if kw < 55 and missing:
        recs.append("JD vocabulary absent from the resume (add ONLY where your real "
                    "work matches): " + ", ".join(missing[:8]) + ".")
    if sections < 100:
        have = [n for n in SECTIONS if tailored.get(n if n != "skills" else "skills")]
        recs.append(f"Missing standard sections: {sorted(set(SECTIONS) - set(have))}.")
    if not recs:
        recs.append("Well aligned. Further keyword work would be stuffing, not signal.")

    s = ATSScore(round(overall, 1), round(kw, 1), round(skills_cov, 1),
                 round(sections, 1), round(parse, 1), round(clarity, 1),
                 round(impact, 1),
                 matched[:25], missing[:25], sorted(injectable)[:15], recs)
    log.info("ats.scored", overall=s.overall, kw=s.keyword_match,
             skills=s.skills_coverage, parse=s.parseability,
             clarity=s.experience_clarity, impact=s.impact_density)
    return s
