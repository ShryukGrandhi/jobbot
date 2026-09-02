"""Tool schemas that force schema-valid structured output.

Every vision checkpoint calls a forced tool rather than parsing prose. Prose
parsing fails in exactly the situation that matters most -- a long, unusual
form -- and fails silently, producing a plausible object with invented fields.
"""

from __future__ import annotations

from typing import Any

FIELD_KINDS = [
    "text", "textarea", "email", "phone", "number", "date",
    "select", "combobox", "multiselect", "radio", "checkbox",
    "file", "consent", "unknown",
]

# -- checkpoint 1: read every question off the page -----------------------

PARSE_FORM_TOOL: dict[str, Any] = {
    "name": "report_form",
    "description": (
        "Report every answerable field visible on this application page, plus "
        "the page's structure. Report only what is actually visible in the "
        "screenshots and the accessibility outline. Never invent a field."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "page_title": {"type": "string"},
            "step": {
                "type": "string",
                "description": "Step label if this is a multi-step wizard, e.g. 'My Information (2 of 5)'.",
            },
            "total_steps": {"type": ["integer", "null"]},
            "requires_account": {
                "type": "boolean",
                "description": "True if the page demands sign-in or account creation before the form is reachable.",
            },
            "submit_label": {
                "type": "string",
                "description": "Exact visible text of the control that advances or submits, e.g. 'Submit Application' or 'Save and Continue'.",
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string", "description": "The id/name attribute if visible, else a short slug of the label."},
                        "label": {"type": "string", "description": "The visible question text, verbatim."},
                        "kind": {"type": "string", "enum": FIELD_KINDS},
                        "required": {"type": "boolean"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Selectable choices, verbatim, for select/radio/combobox/multiselect.",
                        },
                        "help_text": {"type": "string"},
                        "current_value": {"type": "string", "description": "Value already present in the control, if any."},
                        "section": {"type": "string"},
                        "legally_significant": {
                            "type": "boolean",
                            "description": (
                                "True if answering states a legal or contractual fact: work authorization, "
                                "visa sponsorship, citizenship, criminal history, background-check or drug-test "
                                "consent, veteran or disability status, age, education completion, licensure, "
                                "non-compete, or prior employment at this company."
                            ),
                        },
                    },
                    "required": ["field_id", "label", "kind", "required"],
                },
            },
            "notes": {"type": "string", "description": "Anything unusual: blocking modals, captchas, error banners, an already-submitted state."},
        },
        "required": ["fields", "submit_label"],
    },
}

# -- answering ------------------------------------------------------------

ANSWER_FIELDS_TOOL: dict[str, Any] = {
    "name": "answer_fields",
    "description": (
        "Propose an answer for each field you were asked about, grounded strictly "
        "in the candidate profile provided. Compose prose freely, but never assert "
        "a fact the profile does not contain."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string"},
                        "value": {
                            "type": ["string", "boolean", "number", "null"],
                            "description": "For select/radio/combobox use the option text VERBATIM. Null if you cannot answer from the profile.",
                        },
                        "source": {
                            "type": "string",
                            "enum": ["profile", "derived", "composed"],
                            "description": (
                                "profile = copied from a profile field; derived = computed from profile facts "
                                "(e.g. years of experience from dates); composed = prose you wrote from profile content."
                            ),
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string", "description": "One short sentence citing what in the profile supports this."},
                        "needs_human": {
                            "type": "boolean",
                            "description": "True if the profile does not contain what this field asks for. Prefer this over guessing.",
                        },
                    },
                    "required": ["field_id", "value", "source", "confidence", "needs_human"],
                },
            }
        },
        "required": ["answers"],
    },
}

# -- checkpoint 2: verify before submitting -------------------------------

VERIFY_FORM_TOOL: dict[str, Any] = {
    "name": "report_verification",
    "description": (
        "Inspect the filled form as rendered and report whether it is safe to submit. "
        "Compare every visible value against the candidate profile. Report a problem "
        "for anything wrong, missing, misplaced, or truncated."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ready_to_submit": {"type": "boolean"},
            "summary": {"type": "string"},
            "unfilled_required": {
                "type": "array", "items": {"type": "string"},
                "description": "Labels of required fields still empty.",
            },
            "validation_errors": {
                "type": "array", "items": {"type": "string"},
                "description": "Inline error messages the page is displaying, verbatim.",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string"},
                        "label": {"type": "string"},
                        "problem": {"type": "string"},
                        "severity": {"type": "string", "enum": ["blocker", "warning"]},
                        "suggested_value": {"type": ["string", "boolean", "number", "null"]},
                    },
                    "required": ["label", "problem", "severity"],
                },
            },
        },
        "required": ["ready_to_submit", "issues"],
    },
}

# -- checkpoint 3: post-submit, one call only -----------------------------

POST_SUBMIT_TOOL: dict[str, Any] = {
    "name": "report_outcome",
    "description": (
        "Determine from the screenshot whether the application was actually "
        "submitted, and record lessons for future applications. Do not assume "
        "success because a button was clicked -- look for positive evidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "submitted": {
                "type": "boolean",
                "description": "True ONLY with affirmative on-page evidence: a confirmation message, a reference number, or an 'already applied' state.",
            },
            "evidence": {
                "type": "string",
                "description": "The exact on-screen text that proves it, verbatim. Empty if none.",
            },
            "still_on_form": {"type": "boolean"},
            "errors_shown": {"type": "array", "items": {"type": "string"}},
            "answers_correct": {
                "type": "boolean",
                "description": "Whether the answers visible on the confirmation match the profile.",
            },
            "lessons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "observation": {"type": "string"},
                        "fix": {"type": "string", "description": "A concrete, reusable change for next time on this ATS."},
                        "scope": {"type": "string", "enum": ["this_ats", "this_company", "global"]},
                    },
                    "required": ["observation", "fix", "scope"],
                },
            },
        },
        "required": ["submitted", "evidence", "still_on_form"],
    },
}

# -- knockout pre-scan ----------------------------------------------------

KNOCKOUT_TOOL: dict[str, Any] = {
    "name": "report_knockouts",
    "description": (
        "Identify questions whose honest answer, given this candidate's profile, "
        "would trigger automatic rejection. Only structured questions (yes/no, "
        "single-select, multi-select) can auto-reject; free text cannot."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "knockouts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string"},
                        "label": {"type": "string"},
                        "honest_answer": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["label", "honest_answer", "reason"],
                },
            },
            "recommend_skip": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": ["knockouts", "recommend_skip"],
    },
}
