"""Prompt-only rendering for deterministic candidate claim propositions.

The compiler's ``canonical_proposition`` is an identity surface, not display
prose. Atomic anchors retain that raw value as ``claim_text``. This module
converts the known grammar to readable text only when the prompt is rendered;
the result must never be written back to Mongo or anchor metadata.
"""

from __future__ import annotations

import re


_CANONICAL_PATTERN = re.compile(
    r"^(?P<subject>.*?)\s+"
    r"(?P<polarity>POSITIVE|NEGATIVE)\s+"
    r"(?P<modality>"
    r"ASSERTED|POSSIBLE|PROBABLE|NECESSARY|RECOMMENDED|HYPOTHETICAL"
    r")\s+"
    r"(?P<predicate>UNTYPED\[[^\]]+\]|[A-Z][A-Z_]+)"
    r"(?:\s+(?P<tail>.*))?$"
)
_CLAUSE_MARKER = re.compile(r"\s+(IF|EXCEPT|WHEN)\s+")
_UNTYPED = re.compile(r"^UNTYPED\[([^\]]+)\]$")
_MACHINE_TOKEN = re.compile(
    r"\b(?:POSITIVE|NEGATIVE|ASSERTED|POSSIBLE|PROBABLE|NECESSARY|"
    r"RECOMMENDED|HYPOTHETICAL|UNTYPED)\b"
)

# (finite asserted form, infinitive/modal form)
_PREDICATE_FORMS: dict[str, tuple[str, str]] = {
    "CAUSES": ("causes", "cause"),
    "INFLUENCES": ("influences", "influence"),
    "INCREASES": ("increases", "increase"),
    "DECREASES": ("decreases", "decrease"),
    "UPDATES": ("updates", "update"),
    "SIGNALS": ("signals", "signal"),
    "MEASURES": ("measures", "measure"),
    "COMPARES_AGAINST": ("compares against", "compare against"),
    "ENABLES": ("enables", "enable"),
    "INHIBITS": ("inhibits", "inhibit"),
    "REQUIRES": ("requires", "require"),
    "CONSTRAINS": ("constrains", "constrain"),
    "RESULTS_IN": ("results in", "result in"),
    "APPLIES_UNDER": ("applies under", "apply under"),
    "PART_OF": ("is part of", "be part of"),
    "USED_FOR": ("is used for", "be used for"),
    "ASSOCIATED_WITH": ("is associated with", "be associated with"),
}
_MODAL_PREFIX = {
    "POSSIBLE": "may",
    "PROBABLE": "likely",
    "NECESSARY": "must",
    "RECOMMENDED": "should",
    "HYPOTHETICAL": "may",
}


def _clean_fragment(value: str) -> str:
    parts: list[str] = []
    for raw_part in value.split("|"):
        part = re.sub(r"(?<!\w)\*+(?!\w)", " ", raw_part)
        part = re.sub(r"\s+", " ", part).strip(" \t\r\n,;:|*")
        if part:
            parts.append(part)
    return " and ".join(parts)


def _predicate_forms(token: str) -> tuple[str, str]:
    untyped = _UNTYPED.fullmatch(token)
    if untyped:
        lemma = _clean_fragment(untyped.group(1).replace("_", " ")).lower()
        return lemma, lemma
    fallback = token.replace("_", " ").lower()
    return _PREDICATE_FORMS.get(token, (fallback, fallback))


def _clause_parts(tail: str) -> tuple[str, list[tuple[str, str]]]:
    if not tail:
        return "", []
    pieces = _CLAUSE_MARKER.split(tail)
    object_text = _clean_fragment(pieces[0])
    clauses: list[tuple[str, str]] = []
    for index in range(1, len(pieces), 2):
        marker = pieces[index].lower()
        value = _clean_fragment(pieces[index + 1])
        if value:
            clauses.append((marker, value))
    return object_text, clauses


def _finish_sentence(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if not compact:
        return ""
    compact = compact[0].upper() + compact[1:]
    if compact[-1] not in ".!?":
        compact += "."
    return compact


def render_claim_proposition(
    raw_claim_text: str,
    *,
    exact_sentence: str,
) -> str:
    """Return readable prompt prose while preserving the raw input byte-for-byte."""

    raw = re.sub(r"\s+", " ", str(raw_claim_text or "")).strip()
    evidence_fallback = _finish_sentence(str(exact_sentence or ""))
    if not raw:
        return evidence_fallback

    match = _CANONICAL_PATTERN.fullmatch(raw)
    if match is None:
        # Human-readable canonical propositions pass through. A partial machine
        # grammar is safer as exact evidence than as guessed prose.
        if _MACHINE_TOKEN.search(raw) or "[" in raw or "]" in raw:
            return evidence_fallback
        return _finish_sentence(raw)

    subject = _clean_fragment(match.group("subject"))
    if not subject:
        return evidence_fallback
    finite, infinitive = _predicate_forms(match.group("predicate"))
    if not finite or not infinitive:
        return evidence_fallback

    polarity = match.group("polarity")
    modality = match.group("modality")
    object_text, clauses = _clause_parts(match.group("tail") or "")
    if modality == "ASSERTED" and polarity == "POSITIVE":
        predicate_phrase = finite
    elif modality == "ASSERTED":
        predicate_phrase = (
            f"is not {infinitive[3:]}"
            if infinitive.startswith("be ")
            else f"does not {infinitive}"
        )
    else:
        modal = _MODAL_PREFIX.get(modality, "may")
        negation = " not" if polarity == "NEGATIVE" else ""
        predicate_phrase = f"{modal}{negation} {infinitive}"

    proposition = " ".join(
        part for part in (subject, predicate_phrase, object_text) if part
    )
    for marker, value in clauses:
        proposition += f"; {marker} {value}"
    rendered = _finish_sentence(proposition)
    if _MACHINE_TOKEN.search(rendered) or "UNTYPED[" in rendered:
        return evidence_fallback
    return rendered or evidence_fallback
