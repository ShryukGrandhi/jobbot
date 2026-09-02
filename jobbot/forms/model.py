"""Normalized form vocabulary.

This is the seam the whole system turns on: the answering layer speaks in
`FormField` and `ProposedAnswer`, never in DOM handles. Selector rot is then
confined to one adapter per ATS, and the model is never handed a WebElement it
could misidentify.

The bug class this prevents is well documented in prior art. The two
highest-traffic issue threads in one popular applier are "phone number is being
inserted as Last Name" and a phone-country-code mix-up -- neither a selector
bug, both positional heuristics writing data into the wrong box. Binding a label
to its control via `label[for=id]` instead of walking the DOM tree is what makes
that class of error impossible rather than unlikely.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class FieldKind(str, enum.Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    PHONE = "phone"
    NUMBER = "number"
    DATE = "date"
    SELECT = "select"            # native <select>
    COMBOBOX = "combobox"        # react-select / custom listbox
    MULTISELECT = "multiselect"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    FILE = "file"
    CONSENT = "consent"          # attestation / acknowledgement
    UNKNOWN = "unknown"


class AnswerSource(str, enum.Enum):
    PROFILE = "profile"          # verbatim from a confirmed profile field
    DERIVED = "derived"          # computed from profile facts (e.g. YoE)
    COMPOSED = "composed"        # model-written prose grounded in the profile
    HUMAN = "human"


@dataclass
class FieldOption:
    label: str
    value: str = ""

    def __post_init__(self) -> None:
        if not self.value:
            self.value = self.label


@dataclass
class FormField:
    """One answerable control, described without reference to the DOM."""

    field_id: str                       # our stable handle
    label: str
    kind: FieldKind = FieldKind.UNKNOWN
    required: bool = False
    options: list[FieldOption] = field(default_factory=list)
    placeholder: str = ""
    help_text: str = ""
    current_value: str = ""
    group: str = ""                     # section heading, for context
    max_length: int | None = None

    # Adapter-owned. Never shown to the model.
    selector: str = ""
    frame_url: str = ""

    # Set by classification, not by the model.
    profile_key: str | None = None      # which profile field answers this
    legally_significant: bool = False

    def option_labels(self) -> list[str]:
        return [o.label for o in self.options]

    def to_prompt_dict(self) -> dict[str, Any]:
        """The view the model sees. Deliberately excludes `selector`.

        Long option lists are truncated: dumping a 1,000-entry country dropdown
        into context is pure cost, and the model should be snapping to an option
        via the matcher rather than reading them all.
        """
        d: dict[str, Any] = {
            "field_id": self.field_id,
            "label": self.label,
            "kind": self.kind.value,
            "required": self.required,
        }
        if self.group:
            d["section"] = self.group
        if self.help_text:
            d["help"] = self.help_text[:300]
        if self.placeholder:
            d["placeholder"] = self.placeholder[:120]
        if self.current_value:
            d["current_value"] = self.current_value[:200]
        if self.options:
            labels = self.option_labels()
            d["options"] = labels[:60]
            if len(labels) > 60:
                d["options_truncated"] = f"{len(labels)} total; match by meaning"
        if self.legally_significant:
            d["legally_significant"] = True
        return d


@dataclass
class ProposedAnswer:
    field_id: str
    value: Any
    source: AnswerSource
    confidence: float = 0.0
    rationale: str = ""
    needs_human: bool = False
    blocked_reason: str = ""

    @property
    def submittable(self) -> bool:
        return not self.needs_human and self.value not in (None, "")


@dataclass
class ParsedForm:
    """Everything checkpoint 1 learned about the page."""

    fields: list[FormField] = field(default_factory=list)
    submit_label: str = ""
    page_title: str = ""
    step: str = ""
    total_steps: int | None = None
    requires_account: bool = False
    notes: str = ""

    def required_fields(self) -> list[FormField]:
        return [f for f in self.fields if f.required]

    def by_id(self, fid: str) -> FormField | None:
        return next((f for f in self.fields if f.field_id == fid), None)


@dataclass
class Knockout:
    """A question whose honest answer triggers automatic rejection."""

    field_id: str
    label: str
    honest_answer: str
    reason: str


@dataclass
class VerificationIssue:
    field_id: str
    label: str
    problem: str
    severity: str          # "blocker" | "warning"
    suggested_value: Any = None


@dataclass
class Verification:
    """Checkpoint 2's verdict."""

    ready_to_submit: bool = False
    issues: list[VerificationIssue] = field(default_factory=list)
    unfilled_required: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def blockers(self) -> list[VerificationIssue]:
        return [i for i in self.issues if i.severity == "blocker"]
