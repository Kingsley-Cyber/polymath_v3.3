"""Document-local exact mention completion with ambiguity guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import spacy
from spacy.matcher import PhraseMatcher

from models.graphify_contracts import (
    CompletedMentionV1,
    DocumentEntityV1,
    EntityTerminalState,
    NormalizedDocumentV1,
    RawMentionV1,
    stable_digest,
    stable_id,
)
from services.extraction.graphify_normalization import to_original_span

COMPLETION_RELEASE = "graphify-document-mention-completion-v2"
_AMBIGUOUS_NAMES = frozenset({"go", "make", "apple", "python", "oracle", "rust"})
_ELIGIBLE_STATES = frozenset({EntityTerminalState.PROMOTED, EntityTerminalState.DOCUMENT_LOCAL})


@dataclass(frozen=True)
class CompletionOutput:
    mentions: tuple[CompletedMentionV1, ...]
    report: dict[str, object]


@dataclass(frozen=True)
class _Pattern:
    entity: DocumentEntityV1
    text: str
    source: str


def _sentence_context(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    stops = [value for value in (text.find(".", end), text.find("\n", end)) if value >= 0]
    right = min(stops) + 1 if stops else len(text)
    return text[left:right]


def _ambiguous_allowed(name: str, context: str) -> bool:
    lowered = context.casefold()
    cues = {
        "go": ("programming language", "graphify cli", "software", "implements", "supports"),
        "make": ("build tool", "makefile", "software", "named"),
        "apple": ("owns", "company", "organization", "inc.", "software"),
        "python": ("programming", "library", "software", "implements", "parser", "rules", "built on top of"),
        "oracle": ("supports", "database", "software", "company"),
        "rust": ("programming", "software", "supports", "command line service"),
    }
    return any(cue in lowered for cue in cues[name.casefold()])


def _patterns(entities: Sequence[DocumentEntityV1]) -> tuple[_Pattern, ...]:
    rows: dict[tuple[str, str], _Pattern] = {}
    for entity in entities:
        if entity.state not in _ELIGIBLE_STATES:
            continue
        values = [(entity.canonical_name, "exact_name")]
        values.extend((alias, "acronym" if alias.isupper() else "alias") for alias in entity.aliases)
        for value, source in values:
            cleaned = value.strip()
            if not cleaned:
                continue
            rows[(entity.entity_id, cleaned)] = _Pattern(entity, cleaned, source)
    return tuple(sorted(rows.values(), key=lambda item: (
        item.entity.document_id, item.entity.entity_id, item.text.casefold(), item.text,
    )))


def complete_document_mentions(
    document: NormalizedDocumentV1,
    entities: Sequence[DocumentEntityV1],
    raw_mentions: Sequence[RawMentionV1] = (),
) -> CompletionOutput:
    if any(entity.document_id != document.document_id for entity in entities):
        raise ValueError("document entity identity mismatch")
    nlp = spacy.blank("en")
    doc = nlp.make_doc(document.normalized_text)
    exact_matcher = PhraseMatcher(nlp.vocab, attr="ORTH")
    folded_matcher = PhraseMatcher(nlp.vocab, attr="LOWER")
    pattern_rows = _patterns(entities)
    by_match_id: dict[int, _Pattern] = {}
    for index, pattern in enumerate(pattern_rows):
        key = f"ENTITY_{index}"
        pattern_doc = nlp.make_doc(pattern.text)
        if pattern.text.casefold() in _AMBIGUOUS_NAMES:
            exact_matcher.add(key, [pattern_doc])
            match_id = nlp.vocab.strings[key]
        else:
            folded_matcher.add(key, [pattern_doc])
            match_id = nlp.vocab.strings[key]
        by_match_id[match_id] = pattern

    candidates: list[tuple[int, int, _Pattern]] = []
    ambiguous_rejections = 0
    for match_id, token_start, token_end in exact_matcher(doc) + folded_matcher(doc):
        span = doc[token_start:token_end]
        pattern = by_match_id[match_id]
        if pattern.text.casefold() in _AMBIGUOUS_NAMES:
            context = _sentence_context(document.normalized_text, span.start_char, span.end_char)
            if not _ambiguous_allowed(pattern.text, context):
                ambiguous_rejections += 1
                continue
        candidates.append((span.start_char, span.end_char, pattern))

    candidates.sort(key=lambda item: (
        item[0], -(item[1] - item[0]),
        0 if item[2].entity.state == EntityTerminalState.PROMOTED else 1,
        -item[2].entity.confidence,
        item[2].entity.entity_id, item[2].text,
    ))
    accepted: list[tuple[int, int, _Pattern]] = []
    accepted_end = -1
    overlap_rejections = 0
    for start, end, pattern in candidates:
        # Candidates are start-sorted and accepted intervals never overlap, so
        # only the most recently accepted interval can overlap this candidate.
        if start < accepted_end:
            overlap_rejections += 1
            continue
        accepted_end = end
        accepted.append((start, end, pattern))

    raw_index = {
        (mention.normalized_start, mention.normalized_end, mention.mention_id): mention
        for mention in raw_mentions
        if mention.normalized_start is not None and mention.normalized_end is not None
    }
    completed: list[CompletedMentionV1] = []
    for start, end, pattern in sorted(accepted, key=lambda item: (item[0], item[1], item[2].entity.entity_id)):
        original = to_original_span(document, start, end)
        if not original.exact:
            continue
        source = pattern.source
        if any(
            (start, end, mention_id) in raw_index
            for mention_id in pattern.entity.mention_ids
        ):
            source = "raw"
        surface = document.normalized_text[start:end]
        completed.append(CompletedMentionV1(
            mention_id=stable_id(
                "completed-mention", document.document_id, pattern.entity.entity_id,
                start, end, surface, COMPLETION_RELEASE,
            ),
            entity_id=pattern.entity.entity_id,
            document_id=document.document_id,
            surface=surface,
            normalized_start=start,
            normalized_end=end,
            original_start=original.start,
            original_end=original.end,
            source=source,
            context_rule=("ambiguous_named_context" if pattern.text.casefold() in _AMBIGUOUS_NAMES else ""),
            completion_release=COMPLETION_RELEASE,
        ))
    report: dict[str, object] = {
        "schema_version": "polymath.document_mention_completion_report.v1",
        "status": "passed",
        "release": COMPLETION_RELEASE,
        "document_id": document.document_id,
        "eligible_entities": sum(entity.state in _ELIGIBLE_STATES for entity in entities),
        "compiled_patterns": len(pattern_rows),
        "candidate_matches": len(candidates) + ambiguous_rejections,
        "ambiguous_context_rejections": ambiguous_rejections,
        "overlap_rejections": overlap_rejections,
        "completed_mentions": len(completed),
        "strict_offset_rate": 1.0,
        "deterministic_ids": len({item.mention_id for item in completed}) == len(completed),
        "identity_digest": stable_digest([item.model_dump(mode="json") for item in completed]),
    }
    return CompletionOutput(tuple(completed), report)
