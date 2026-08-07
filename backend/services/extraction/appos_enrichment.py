"""spaCy appositional classification for the deterministic alias pipeline.

Phase 3 (alias directive): classify appositions into typed observations.
Length alone must NEVER decide alias-hood.

Classes:
  explicit_name_alias      → proper-name / "known as" alias candidate
  descriptive_common_noun  → description/type (not an alias)
  role                     → role relation (not an alias)
  location                 → location/type relation (not an alias)
  ambiguous_name_like      → review candidate (name-like, no explicit signal)

Legacy ``spacy_appos_enrichment`` remains for shape compatibility but now
respects this policy (descriptions never enter the aliases dict).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

_NLP: Any = None

AppositionClass = Literal[
    "explicit_name_alias",
    "descriptive_common_noun",
    "role",
    "location",
    "ambiguous_name_like",
]

_KNOWN_AS_RE = re.compile(
    r"\b(?:also\s+)?(?:known|called)\s+as\b|\ba\.?k\.?a\.?\b|\bnée\b|\bnee\b",
    re.IGNORECASE,
)
# "X, also known as Y" — spaCy often does NOT mark this as dep=appos.
_KNOWN_AS_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<head>[A-Z][A-Za-z0-9'&.\-]*(?:\s+[A-Z][A-Za-z0-9'&.\-]*){0,5})"
    r"(?![A-Za-z0-9])\s*,?\s*"
    r"(?:(?:also\s+)?(?:known|called)\s+as|a\.?k\.?a\.?)\s+"
    r"(?P<alias>(?:the\s+)?[A-Za-z0-9][A-Za-z0-9'&.\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&.\-]*){0,5})"
    r"(?=[,.;:!?\n]|$)",
    re.IGNORECASE,
)
# Gap between entity and appositive may only be punctuation / known-as cue.
_APPOS_GAP_RE = re.compile(
    r"^[\s,;:()\[\]\"'“”‘’\-–—]*$"
    r"|^(?:[\s,;:()\[\]\"'“”‘’\-–—]*"
    r"(?:(?:also\s+)?(?:known|called)\s+as|a\.?k\.?a\.?)"
    r"[\s,;:()\[\]\"'“”‘’\-–—]*)$",
    re.IGNORECASE,
)
_ROLE_RE = re.compile(
    r"\b(?:CEO|CFO|CTO|COO|president|founder|co-?founder|director|chairman|"
    r"chairwoman|chief\s+\w+\s+officer|minister|senator|governor|mayor|"
    r"coach|captain|manager|secretary|ambassador)\b",
    re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r"\b(?:capital|city|town|village|state|province|county|region|country|"
    r"island|peninsula)\s+of\b|\b(?:northern|southern|eastern|western)\b",
    re.IGNORECASE,
)
_LEADING_DET_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
_PROPER_TOKEN_RE = re.compile(r"^[A-Z][\w''-]*$")


@dataclass(frozen=True)
class AppositionObservation:
    """One classified apposition with dual source spans."""

    class_name: AppositionClass
    entity_canonical: str
    entity_surface: str
    appos_text: str
    entity_start: int
    entity_end: int
    appos_start: int
    appos_end: int
    evidence_text: str
    sentence_text: str
    rule_id: str


def get_shared_nlp() -> Any:
    """Get or create the shared spaCy nlp singleton.

    All extraction modules (appos_enrichment, dep_path_extractor,
    spacy_relation_adapter) MUST use this single instance to avoid
    duplicate model loads and enable Doc sharing.
    """
    global _NLP
    if _NLP is not None:
        return _NLP
    import spacy

    model = os.environ.get("SPACY_MODEL", "en_core_web_sm")
    # GLiNER owns entities; spaCy ner/textcat are dead weight.
    _NLP = spacy.load(model, disable=["ner", "textcat"])
    return _NLP


def _get_nlp() -> Any:
    """Backwards-compatible alias."""
    return get_shared_nlp()


def _np_tokens(tok) -> list:
    """Noun-phrase tokens under tok, excluding relative clauses and appositives."""
    np_tokens = []
    for t in sorted(tok.subtree, key=lambda x: x.i):
        if t.pos_ in ("VERB", "AUX") and t.dep_ == "relcl":
            break
        # Do not absorb nested appositives into the head NP span/text.
        if t.dep_ == "appos" and t.head == tok:
            continue
        if any(a.dep_ == "appos" and a.head == tok for a in t.ancestors):
            continue
        np_tokens.append(t)
    return np_tokens


def _extract_np_text(tok) -> str:
    """Extract the full noun phrase rooted at tok (including children)."""
    np_tokens = _np_tokens(tok)
    if not np_tokens:
        return ""
    parts = [np_tokens[0].text]
    for prev, cur in zip(np_tokens, np_tokens[1:]):
        if cur.idx == prev.idx + len(prev.text):
            parts.append(cur.text)
        else:
            parts.append(" " + cur.text)
    return "".join(parts).strip()


def _np_char_span(tok) -> tuple[int, int] | None:
    ordered = _np_tokens(tok)
    if not ordered:
        return None
    start = ordered[0].idx
    end = ordered[-1].idx + len(ordered[-1].text)
    if end <= start:
        return None
    return start, end


def _looks_proper_name(phrase: str) -> bool:
    tokens = [t for t in re.split(r"\s+", phrase.strip()) if t]
    if not tokens:
        return False
    # Strip leading determiner for the proper-name check only.
    if tokens[0].lower() in {"a", "an", "the"} and len(tokens) > 1:
        tokens = tokens[1:]
    if not tokens:
        return False
    # Require majority Title/UPPER tokens and no obvious common-noun filler.
    properish = sum(1 for t in tokens if _PROPER_TOKEN_RE.match(t.strip(",.;:")))
    return properish >= max(1, (len(tokens) + 1) // 2)


def classify_apposition_phrase(
    appos_text: str,
    *,
    sentence_text: str = "",
    left_context: str = "",
) -> tuple[AppositionClass, str]:
    """Classify an appositive phrase. Length is never used as the decision.

    Returns ``(class_name, rule_id)``.
    """

    phrase = (appos_text or "").strip()
    if not phrase:
        return "descriptive_common_noun", "APPOS_EMPTY_V1"

    window = f"{left_context} {phrase} {sentence_text}".strip()

    if _KNOWN_AS_RE.search(left_context) or _KNOWN_AS_RE.search(phrase):
        return "explicit_name_alias", "APPOS_KNOWN_AS_V1"

    if _ROLE_RE.search(phrase) or _ROLE_RE.search(window):
        return "role", "APPOS_ROLE_V1"

    if _LOCATION_RE.search(phrase):
        return "location", "APPOS_LOCATION_V1"

    # Leading determiner + common descriptive content → description.
    if _LEADING_DET_RE.match(phrase):
        remainder = _LEADING_DET_RE.sub("", phrase).strip()
        if remainder and not _looks_proper_name(remainder):
            return "descriptive_common_noun", "APPOS_DESCRIPTIVE_DET_V1"
        if remainder and _looks_proper_name(remainder):
            # "the Weeknd" after known-as already handled; bare "the X" proper → review
            return "ambiguous_name_like", "APPOS_DET_PROPER_REVIEW_V1"

    if _looks_proper_name(phrase):
        return "ambiguous_name_like", "APPOS_PROPER_REVIEW_V1"

    return "descriptive_common_noun", "APPOS_DESCRIPTIVE_DEFAULT_V1"


def _entity_spans_from_rows(
    text: str, entities: list[dict]
) -> list[tuple[int, int, str, str]]:
    """Build (start, end, canonical_lower, surface) rows; infer spans when missing."""

    ent_by_span: list[tuple[int, int, str, str]] = []
    for ent in entities:
        canon = (ent.get("canonical_name") or ent.get("surface_form") or "").strip()
        surface = (ent.get("surface_form") or canon).strip()
        if not canon:
            continue
        start = ent.get("start_char")
        end = ent.get("end_char")
        if start is None:
            start = ent.get("start")
        if end is None:
            end = ent.get("end")
        if start is not None and end is not None and int(end) > int(start):
            ent_by_span.append((int(start), int(end), canon.lower(), surface))
            continue
        # Infer from source text (first exact, then case-insensitive).
        needle = surface or canon
        pos = text.find(needle)
        if pos < 0:
            pos = text.lower().find(needle.lower())
        if pos >= 0:
            ent_by_span.append((pos, pos + len(needle), canon.lower(), surface or needle))
    return ent_by_span


def _gap_ok(text: str, left_end: int, right_start: int) -> bool:
    if right_start < left_end:
        left_end, right_start = right_start, left_end
    gap = text[left_end:right_start]
    return bool(_APPOS_GAP_RE.match(gap))


def _sentence_for_offset(doc: Any, offset: int) -> str:
    for sent in doc.sents:
        if sent.start_char <= offset < sent.end_char:
            return sent.text.strip()[:300]
    return ""


def extract_apposition_observations(
    text: str,
    entities: list[dict],
    *,
    doc: Any = None,
) -> list[AppositionObservation]:
    """Return typed apposition observations with dual spans.

    Reuses a shared spaCy Doc when provided (parse-once invariant).
    """

    if not (text or "").strip() or not entities:
        return []

    if doc is None:
        nlp = _get_nlp()
        doc = nlp(text)

    ent_by_span = _entity_spans_from_rows(text, entities)
    if not ent_by_span:
        return []

    def _find_entity_for_token(tok) -> tuple[int, int, str, str] | None:
        for start, end, canon, surface in ent_by_span:
            if tok.idx >= start and tok.idx < end:
                return start, end, canon, surface
        return None

    def _find_entity_covering(start: int, end: int) -> tuple[int, int, str, str] | None:
        for e_start, e_end, canon, surface in ent_by_span:
            if e_start <= start and end <= e_end:
                return e_start, e_end, canon, surface
            if start <= e_start and e_end <= end:
                return e_start, e_end, canon, surface
            # Allow exact surface match window.
            if e_start == start or e_end == end:
                return e_start, e_end, canon, surface
        return None

    observations: list[AppositionObservation] = []
    seen: set[tuple[str, str, int, int]] = set()

    def _append_obs(
        *,
        class_name: AppositionClass,
        rule_id: str,
        entity_start: int,
        entity_end: int,
        canon: str,
        surface: str,
        appos_text: str,
        appos_start: int,
        appos_end: int,
        sentence_text: str,
    ) -> None:
        if not appos_text or len(appos_text.strip()) < 2:
            return
        if appos_end <= appos_start or entity_end <= entity_start:
            return
        if not _gap_ok(text, entity_end, appos_start) and not _gap_ok(
            text, appos_end, entity_start
        ):
            return
        key = (canon, appos_text.lower(), appos_start, entity_start)
        if key in seen:
            return
        seen.add(key)
        lo = min(entity_start, appos_start)
        hi = max(entity_end, appos_end)
        observations.append(
            AppositionObservation(
                class_name=class_name,
                entity_canonical=canon,
                entity_surface=surface,
                appos_text=appos_text,
                entity_start=entity_start,
                entity_end=entity_end,
                appos_start=appos_start,
                appos_end=appos_end,
                evidence_text=text[lo:hi],
                sentence_text=sentence_text,
                rule_id=rule_id,
            )
        )

    # 1) Explicit "known as" / a.k.a. spans (spaCy often misses these as appos).
    for match in _KNOWN_AS_SPAN_RE.finditer(text):
        head = match.group("head").strip()
        alias = match.group("alias").strip()
        head_span = (match.start("head"), match.end("head"))
        alias_span = (match.start("alias"), match.end("alias"))
        ent = _find_entity_covering(*head_span)
        if ent is None:
            # Entity may be the alias side ("the Weeknd" as canonical).
            ent = _find_entity_covering(*alias_span)
            if ent is None:
                continue
            entity_start, entity_end, canon, surface = ent
            # Alias is the entity; head is the other name → still explicit alias.
            appos_text, appos_start, appos_end = head, head_span[0], head_span[1]
        else:
            entity_start, entity_end, canon, surface = ent
            appos_text, appos_start, appos_end = alias, alias_span[0], alias_span[1]
        left_context = text[min(entity_end, appos_start) : max(entity_end, appos_start)]
        class_name, rule_id = classify_apposition_phrase(
            appos_text,
            sentence_text=_sentence_for_offset(doc, entity_start),
            left_context=left_context,
        )
        # Force explicit when the cue matched — classifier should agree.
        if class_name != "explicit_name_alias":
            class_name, rule_id = "explicit_name_alias", "APPOS_KNOWN_AS_SPAN_V1"
        _append_obs(
            class_name=class_name,
            rule_id=rule_id,
            entity_start=entity_start,
            entity_end=entity_end,
            canon=canon,
            surface=surface,
            appos_text=appos_text,
            appos_start=appos_start,
            appos_end=appos_end,
            sentence_text=_sentence_for_offset(doc, entity_start),
        )

    # 2) Dependency appositions (comma/parenthetical NPs).
    for tok in doc:
        for child in tok.children:
            if child.dep_ != "appos":
                continue
            ent_match = _find_entity_for_token(tok) or _find_entity_for_token(tok.head)
            if ent_match is None:
                continue
            entity_start, entity_end, canon, surface = ent_match
            appos_text = _extract_np_text(child)
            span = _np_char_span(child)
            if span is None:
                continue
            left_context = text[entity_end: child.idx]
            class_name, rule_id = classify_apposition_phrase(
                appos_text,
                sentence_text=child.sent.text.strip(),
                left_context=left_context,
            )
            _append_obs(
                class_name=class_name,
                rule_id=rule_id,
                entity_start=entity_start,
                entity_end=entity_end,
                canon=canon,
                surface=surface,
                appos_text=appos_text,
                appos_start=span[0],
                appos_end=span[1],
                sentence_text=child.sent.text.strip()[:300],
            )

        if tok.dep_ == "appos":
            ent_match = _find_entity_for_token(tok)
            if ent_match is None:
                continue
            entity_start, entity_end, canon, surface = ent_match
            head_tok = tok.head
            head_text = _extract_np_text(head_tok)
            span = _np_char_span(head_tok)
            if not head_text or span is None:
                continue
            left_context = (
                text[head_tok.idx: entity_start] if head_tok.idx < entity_start else ""
            )
            class_name, rule_id = classify_apposition_phrase(
                head_text,
                sentence_text=tok.sent.text.strip(),
                left_context=left_context,
            )
            _append_obs(
                class_name=class_name,
                rule_id=rule_id,
                entity_start=entity_start,
                entity_end=entity_end,
                canon=canon,
                surface=surface,
                appos_text=head_text,
                appos_start=span[0],
                appos_end=span[1],
                sentence_text=tok.sent.text.strip()[:300],
            )

    observations.sort(
        key=lambda o: (o.entity_start, o.appos_start, o.class_name, o.appos_text.lower())
    )
    return observations


def spacy_appos_enrichment(
    text: str,
    entities: list[dict],
    *,
    doc: Any = None,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Legacy shape adapter — policy-corrected (Phase 3).

    Only ``explicit_name_alias`` observations enter ``aliases``.
    Descriptive / role / location phrases become definitional_phrases only.
    Ambiguous name-like appositions are omitted from aliases (review path).
    """

    observations = extract_apposition_observations(text, entities, doc=doc)
    aliases: dict[str, list[str]] = {}
    defs: dict[str, str] = {}

    for obs in observations:
        if obs.class_name == "explicit_name_alias":
            aliases.setdefault(obs.entity_canonical, [])
            if obs.appos_text.lower() not in {
                a.lower() for a in aliases[obs.entity_canonical]
            }:
                aliases[obs.entity_canonical].append(obs.appos_text)
        if obs.class_name in {
            "descriptive_common_noun",
            "role",
            "location",
        }:
            if obs.entity_canonical not in defs and obs.sentence_text:
                defs[obs.entity_canonical] = obs.sentence_text[:200]

    for canon in aliases:
        aliases[canon] = aliases[canon][:5]
    return aliases, defs
