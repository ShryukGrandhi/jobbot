"""The candidate profile: the single source of truth for every answer.

Design rule, and the most important one in this codebase:

    The model may compose prose. It may never invent a fact.

Screening questions carry legal and contractual weight. Work authorization,
sponsorship, criminal history, background-check and drug-test consent, veteran
and disability status, age, and education claims are all statements the
candidate makes under their own name -- often under an explicit attestation of
truthfulness. A wrong answer is not a bug, it is a false statement on a job
application.

So every field here is typed by provenance:

    CONFIRMED -- the user stated it. Usable.
    INFERRED  -- derived by the system. Usable ONLY if not legally significant.
    MISSING   -- unknown. Never guessed; the application halts and asks.

Prior art motivates this directly. One widely-used open-source applier shipped
hardcoded felony and background-check answers for every user until a reviewer
caught it; a popular commercial extension is documented defaulting unknown
yes/no screeners to "yes" and submitting anyway. Both are the same bug: a
system answering a legally significant question it was never told the answer to.
"""

from __future__ import annotations

import enum
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, EmailStr, Field, field_validator


class Provenance(str, enum.Enum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    MISSING = "missing"


# Questions whose answers carry legal or contractual weight. The system will
# never emit an answer for one of these unless the user CONFIRMED it. This is a
# denylist for inference, not for storage.
LEGALLY_SIGNIFICANT = frozenset({
    "work_authorization",
    "requires_sponsorship_now",
    "requires_sponsorship_future",
    "visa_status",
    "criminal_history",
    "background_check_consent",
    "drug_test_consent",
    "veteran_status",
    "disability_status",
    "date_of_birth",
    "age_over_18",
    "government_clearance",
    "non_compete",
    "previously_employed_here",
    "related_to_employee",
    "education_degree",
    "education_completed",
    "professional_license",
    "citizenship",
    "arbitration_agreement",
    "policy_acknowledgement",
    "previously_interviewed_here",
})


# The subset a US tech application realistically always asks. These must be
# confirmed before an autonomous run starts. The rest of LEGALLY_SIGNIFICANT is
# still never guessed -- it simply blocks at the point a form actually asks,
# rather than preventing the run outright. Demanding a date of birth up front to
# apply for a software job would be both useless and inappropriate.
CORE_SCREENING = frozenset({
    "work_authorization",
    "requires_sponsorship_now",
    "requires_sponsorship_future",
    "criminal_history",
    "background_check_consent",
    "veteran_status",
    "disability_status",
    "age_over_18",
    "education_degree",
    "previously_employed_here",
})


class Answer(BaseModel):
    """One profile datum plus how we came to know it."""

    value: Any = None
    provenance: Provenance = Provenance.MISSING
    sensitive: bool = False
    note: str | None = None

    @property
    def usable(self) -> bool:
        return self.provenance == Provenance.CONFIRMED and self.value not in (None, "")

    def usable_for(self, key: str) -> bool:
        """May this answer be submitted for the question named `key`?"""
        if self.value in (None, ""):
            return False
        if key in LEGALLY_SIGNIFICANT:
            return self.provenance == Provenance.CONFIRMED
        return self.provenance in (Provenance.CONFIRMED, Provenance.INFERRED)


class Location(BaseModel):
    city: str = ""
    state: str = ""
    country: str = "United States"
    postal_code: str = ""
    street: str = ""


class WorkExperience(BaseModel):
    company: str
    title: str
    start: date
    end: date | None = None          # None == present
    location: str = ""
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    # Free-text explanation used when a gap precedes this role. ~48% of
    # employers auto-screen gaps over six months; explaining one measured a
    # 6.8% vs 4.3% callback rate in a 36,510-opening field experiment.
    gap_explanation: str | None = None

    @property
    def is_current(self) -> bool:
        return self.end is None


class Education(BaseModel):
    school: str
    degree: str
    field_of_study: str = ""
    start: date | None = None
    end: date | None = None
    gpa: float | None = None
    completed: bool = True


class Project(BaseModel):
    name: str
    description: str
    url: str | None = None
    repo: str | None = None
    tech: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    generated: bool = False          # built by this system for a specific role
    generated_for: str | None = None


class Compensation(BaseModel):
    target_base: int | None = None
    minimum_base: int | None = None
    currency: str = "USD"

    def range_string(self, published_min: int | None = None,
                     published_max: int | None = None) -> str:
        """A 'bolstering range': target at the bottom, ~15% above at the top.

        Where the posting publishes a band (now mandatory in 18 US states plus
        DC), anchor inside its upper half instead of guessing.
        """
        if published_min and published_max:
            lo = int(published_min + (published_max - published_min) * 0.5)
            hi = published_max
        elif self.target_base:
            lo = self.target_base
            hi = int(self.target_base * 1.15)
        else:
            return ""
        return f"${lo:,} - ${hi:,}"


class Identity(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    phone: str = ""
    location: Location = Field(default_factory=Location)
    linkedin: str | None = None
    github: str | None = None
    website: str | None = None
    # No photo field, deliberately. A photo costs women 20-30% of callbacks
    # regardless of attractiveness (Ruffle & Shtudiner, Management Science) and
    # amplifies ethnic and religious discrimination. US resumes should omit it.

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Profile(BaseModel):
    """The complete candidate record."""

    identity: Identity
    headline: str = ""
    summary: str = ""
    experience: list[WorkExperience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    # Awards, publications and selective programs. These were being silently
    # dropped, which threw away the strongest differentiators a junior candidate
    # has: peer-reviewed work and competitive wins are expensive to fake and
    # cheap for a recruiter to verify -- exactly the signals that still carry
    # weight when generated prose no longer does.
    awards: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    compensation: Compensation = Field(default_factory=Compensation)

    # Every legally significant answer lives here, and nowhere else.
    screening: dict[str, Answer] = Field(default_factory=dict)

    # Preferences that drive the fit filter.
    target_titles: list[str] = Field(default_factory=list)
    target_locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    onsite_ok: bool = True
    hybrid_ok: bool = True
    willing_to_relocate: bool = False
    min_requirement_match: float = 0.5

    # Ordinary preference answers. Not legally significant, but forms ask them
    # constantly and without them the model correctly refuses to answer -- which
    # leaves required fields empty and blocks the whole application.
    earliest_start: str = ""          # e.g. "2 weeks from offer"
    notice_period_weeks: int | None = None
    work_preference: str = ""         # "remote" | "hybrid" | "onsite" | "flexible"
    timeline_notes: str = ""
    how_heard: str = ""               # "Company website"
    why_this_company_notes: str = ""  # raw material, not a canned answer   # apply at >=50% of listed requirements

    @field_validator("screening", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> Any:
        """Allow plain scalars in YAML; treat them as user-confirmed."""
        if not isinstance(v, dict):
            return v
        out: dict[str, Any] = {}
        for k, raw in v.items():
            if isinstance(raw, dict) and "value" in raw:
                out[k] = raw
            else:
                out[k] = {
                    "value": raw,
                    "provenance": Provenance.CONFIRMED,
                    "sensitive": k in LEGALLY_SIGNIFICANT,
                }
        return out

    # -- lookup -----------------------------------------------------------

    def answer(self, key: str) -> Answer:
        return self.screening.get(key, Answer())

    def can_answer(self, key: str) -> bool:
        return self.answer(key).usable_for(key)

    def missing_legally_significant(self, keys: frozenset[str] | None = None) -> list[str]:
        """Screening keys with no confirmed answer.

        Defaults to CORE_SCREENING -- the questions essentially every
        application asks. The orchestrator refuses to start autonomously while
        any of these is unset. Rarer keys are not preflight blockers, but they
        are still never guessed: they halt that one application if asked.
        """
        return sorted(k for k in (keys or CORE_SCREENING) if not self.can_answer(k))

    def preferences_digest(self) -> str:
        """Preference facts the model may draw on for non-legal questions."""
        loc = self.identity.location
        bits = [
            f"Based in: {loc.city}, {loc.state}, {loc.country}",
            f"Open to remote: {self.remote_ok}",
            f"Open to onsite: {self.onsite_ok}",
            f"Open to hybrid: {self.hybrid_ok}",
            f"Willing to relocate: {self.willing_to_relocate}",
        ]
        if self.work_preference:
            bits.append(f"Preferred arrangement: {self.work_preference}")
        if self.target_locations:
            bits.append(f"Target locations: {', '.join(self.target_locations)}")
        if self.earliest_start:
            bits.append(f"Earliest start: {self.earliest_start}")
        if self.notice_period_weeks is not None:
            bits.append(f"Notice period: {self.notice_period_weeks} weeks")
        if self.timeline_notes:
            bits.append(f"Timeline notes: {self.timeline_notes}")
        if self.how_heard:
            bits.append(f"How they heard about roles: {self.how_heard}")
        if self.identity.website:
            bits.append(f"Personal website: {self.identity.website}")
        if self.why_this_company_notes:
            bits.append(f"Motivation notes: {self.why_this_company_notes}")
        if self.compensation.target_base:
            bits.append(f"Target base salary: {self.compensation.target_base} "
                        f"{self.compensation.currency}")
        return "\n".join(bits)

    @property
    def total_years_experience(self) -> float:
        """Calendar years worked, merging overlapping roles.

        Summing role durations double-counts concurrent jobs -- three
        simultaneous roles over nine months would report ~2.2 years. That number
        feeds years-of-experience dropdowns, which CAN drive an automatic
        rejection, so overstating it is a false answer on a real application.
        Merge the intervals and measure the union instead.
        """
        if not self.experience:
            return 0.0
        spans = sorted(
            (e.start, e.end or date.today()) for e in self.experience
        )
        merged: list[list[date]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        days = sum(max(0, (b - a).days) for a, b in merged)
        return round(days / 365.25, 1)

    def employment_gaps(self, threshold_days: int = 183) -> list[tuple[date, date]]:
        """Gaps over ~6 months, the threshold ~48% of employers auto-screen on."""
        spans = sorted(
            ((e.start, e.end or date.today()) for e in self.experience),
            key=lambda s: s[0],
        )
        gaps: list[tuple[date, date]] = []
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            if (next_start - prev_end).days > threshold_days:
                gaps.append((prev_end, next_start))
        return gaps

    # -- io ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        data = yaml.safe_load(Path(path).read_text())
        return cls.model_validate(data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, width=100)
        )
