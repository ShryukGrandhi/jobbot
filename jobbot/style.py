"""Shared writing rules for everything the system says in the candidate's voice.

Adapted from the no-ai-slop skill (github.com/petergyang/no-ai-slop, MIT),
narrowed to what matters for job applications.

This is not cosmetic. The measured finding is that recruiters reject on
*genericness*, not on AI authorship, and that once tailored prose became free
its correlation with callbacks fell by about half. A cover-letter answer that
could be pasted into any other company's form is worth nothing, however fluent.

The portability test is the load-bearing rule: if a sentence survives being
moved to a different candidate, company, or role unchanged, it is filler.
"""

BANNED_WORDS = [
    "delve", "foster", "leverage", "utilize", "facilitate", "empower",
    "streamline", "robust", "cutting-edge", "paradigm shift", "game changer",
    "tapestry", "realm", "beacon", "multifaceted", "meticulous", "intricate",
    "paramount", "transformative", "elevate", "embark", "supercharge",
    "harness", "ever-evolving", "passionate about", "results-driven",
    "spearheaded", "synergy", "best-in-class", "world-class",
]

STYLE_RULES = """WRITING STYLE — these apply to every word you write in the candidate's voice.

The test that matters: if a sentence could be moved unchanged to a different
candidate, a different company, or a different role, it is filler. Cut it or
replace it with a fact, mechanism, number, or judgment specific to this one.

Never write:
- Binary contrasts. "It's not X, it's Y." State Y.
- Throat-clearing. "Here's the thing," "Let me be clear," "I'll be honest."
- Faux-insight. "What most people miss," "Here's what nobody tells you."
- Colon reveals. "The best part: it learns." Write a plain sentence.
- Importance puffery. "a testament to," "marks a pivotal moment," "plays a
  vital role." State the fact; let the reader judge.
- Superficial -ing clauses. "..., highlighting my commitment to innovation."
- Weasel attribution. "studies show," "experts agree."
- Fake-profound endings. Do not land on an aphorism or mic-drop line. End on
  the clearest concrete sentence you have.
- Summary-recap endings. "In conclusion," "Ultimately," "Overall."
- Dramatic fragments. "That's it. That's the whole thing."
- Rhetorical setups. "What if I told you," "Think about it:".

Banned words: %s.

Also:
- Active voice. "I shipped it in March," not "it was shipped."
- Make verbs do the work. "decided," not "made a decision."
- Concrete beats abstract. "Cut p99 from 840ms to 95ms" beats "improved
  performance significantly."
- Em dashes: at most one in a short answer, none if a comma or period works.
- Vary sentence shape. Do not stack identical rhythms or punchy fragments.
- Keep contractions and a natural spoken cadence. This should read like a
  competent person typing, not like a press release.
- Do not tell the reader what to notice. Show the fact and stop.
""" % ", ".join(BANNED_WORDS)


def slop_check(text: str) -> list[str]:
    """Cheap mechanical scan. Catches the patterns worth blocking outright."""
    import re

    problems: list[str] = []
    low = (text or "").lower()

    for w in BANNED_WORDS:
        # Match inflections too: "leverage" must also catch "leveraged",
        # "leveraging", "leverages".
        stem = re.escape(w)
        if re.search(rf"(?<!\w){stem}(?:s|d|ed|ing)?(?!\w)", low):
            problems.append(f"banned word: {w!r}")

    patterns = [
        (r"\bit'?s not (just )?\w+[,.] it'?s\b", "binary contrast"),
        (r"\bthe question isn'?t\b", "binary contrast"),
        (r"\bhere'?s the thing\b", "throat-clearing"),
        (r"\blet me be clear\b", "throat-clearing"),
        (r"\bwhat (most people|nobody) (get wrong|tells you|miss)", "faux-insight"),
        (r"\ba testament to\b", "importance puffery"),
        (r"\bmarks a pivotal\b", "importance puffery"),
        (r"\b(studies show|experts agree)\b", "weasel attribution"),
        (r"\b(in conclusion|ultimately,|overall,)", "recap ending"),
        (r"\bwhat if i told you\b", "rhetorical setup"),
        (r"\bat the end of the day\b", "empty phrase"),
        (r"\bit'?s worth noting\b", "empty phrase"),
        (r"\bin today'?s world\b", "empty phrase"),
    ]
    for pat, name in patterns:
        if re.search(pat, low):
            problems.append(name)

    if text.count("—") > 2:
        problems.append(f"em dash overuse ({text.count('—')})")

    return problems
