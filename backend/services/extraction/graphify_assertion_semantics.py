"""Clause-local deterministic assertion-status rules shared by extraction lanes."""

from __future__ import annotations

import re

ASSERTION_SEMANTICS_RELEASE = "graphify-clause-assertion-semantics-v1"

_ASSERTION_NOUNS = (
    "allegation", "assertion", "claim", "hypothesis", "proposal", "report",
    "rumor", "statement",
)
_FALSE_MODIFIERS = (
    "false", "incorrect", "inaccurate", "invalid", "mistaken", "rejected",
    "unsupported", "unsubstantiated", "unverified", "wrong",
)
_REJECTION_VERBS = (
    "debunked", "denied", "disproved", "invalidated", "refuted", "rejected",
)
# Lemma sets for dependency-based claim-noun scope (shared with the relation
# lanes). A proposition embedded under one of these nouns is attributed, never
# a direct assertion; a false modifier or a rejecting matrix verb makes it a
# reported-false claim.
ASSERTION_NOUN_LEMMAS = frozenset({
    *_ASSERTION_NOUNS, "belief", "notion", "suggestion", "idea", "view", "rumour",
})
FALSE_MODIFIER_LEMMAS = frozenset(_FALSE_MODIFIERS)
CLAIM_REJECTION_LEMMAS = frozenset({
    "debunk", "deny", "disprove", "invalidate", "refute", "reject",
    "retract", "withdraw", "dispute",
})

_ASSERTION_NOUN_RE = "(?:" + "|".join(_ASSERTION_NOUNS) + ")"
_FALSE_MODIFIER_RE = "(?:" + "|".join(_FALSE_MODIFIERS) + ")"
_REJECTION_VERB_RE = "(?:" + "|".join(_REJECTION_VERBS) + ")"
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _surface_pattern(value: str) -> str:
    tokens = _WORD_RE.findall(value)
    return r"\s+".join(re.escape(token) for token in tokens)


def nominal_assertion_qualification(
    evidence_text: str,
    subject: str,
    relation: str,
    obj: str,
) -> tuple[str, str, str] | None:
    """Classify a proposition specifically embedded under an assertion noun.

    The proposition surface must occur inside the noun's own ``that`` clause.
    This keeps the rule clause-local and prevents a cue elsewhere in the same
    sentence from contaminating an independent proposition.
    """

    surfaces = tuple(_surface_pattern(value) for value in (subject, relation, obj))
    if not all(surfaces):
        return None
    proposition = rf"{surfaces[0]}\s+{surfaces[1]}\s+{surfaces[2]}"
    pattern = re.compile(
        rf"(?:(?P<modifier>{_FALSE_MODIFIER_RE})\s+)?"
        rf"(?P<noun>{_ASSERTION_NOUN_RE})\s+that\s+{proposition}",
        re.I,
    )
    for match in pattern.finditer(evidence_text):
        noun = match.group("noun").casefold()
        preceding = evidence_text[max(0, match.start() - 160):match.start()]
        rejected = bool(match.group("modifier")) or bool(
            re.search(rf"\b{_REJECTION_VERB_RE}\b[^.!?]{{0,140}}$", preceding, re.I)
        )
        return (
            "denied" if rejected else "positive",
            "asserted",
            f"nominal:{noun}",
        )
    return None
