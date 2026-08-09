"""Deterministic entity-linking ladder for raw OpenIE arguments.

Owner-ratified alignment contract (2026-08-07): an OpenIE argument surface
does not have to equal an entity mention surface — it only has to establish
that it unambiguously refers to that local mention. Resolution walks a
strict ladder; every rung requires a UNIQUE entity or it fails to the next:

    1. exact completed mention surface
    2. exact canonical name / explicit alias
    3. unique completed mention contained inside the argument
    4. unique canonical/alias phrase contained inside the argument
    5. explicit document variant (contiguous name-token short form)
    → otherwise the argument stays non-entity (never invented, never guessed)

No fuzzy matching, no embeddings, no edit distance. Both surfaces are
preserved: `surface` keeps OpenIE's observation verbatim; the mention and
canonical name ride alongside it.
"""

from __future__ import annotations

import re
from bisect import bisect_left, insort
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Sequence

from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1,
    CompletedMentionV1,
    DocumentEntityV1,
    EntityTerminalState,
    OpenIEArgumentKind,
    OpenIERawPropositionV1,
    stable_digest,
    stable_id,
)

ARGUMENT_ADAPTER_RELEASE = "graphify-openie-argument-adapter-v2-deterministic-ladder"
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_PRONOUNS = frozenset({"it", "they", "this", "that", "these", "those", "we", "he", "she", "them", "their", "its"})
_GENERIC = frozenset({
    "thing", "result", "information", "data", "software", "service", "system",
    "component", "process", "method", "approach", "owner", "goal", "end goal",
})
_DATE_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|minutes?|hours?|days?|weeks?|months?|years?)|"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,\s*\d{4})?|\d{4})$",
    re.I,
)
_EMBEDDED_RE = re.compile(r"\b(?:that|whether|because|although|unless|while|if)\b", re.I)
_ELIGIBLE_STATES = frozenset({EntityTerminalState.PROMOTED, EntityTerminalState.DOCUMENT_LOCAL})


@dataclass(frozen=True)
class ArgumentAdapterOutput:
    arguments: tuple[AdaptedOpenIEArgumentV1, ...]
    report: dict[str, object]


@lru_cache(maxsize=262_144)
def _normalize(value: str) -> str:
    # Pure function of its input — caching is behavior-neutral. At book scale
    # the ladder normalizes the same surfaces/canonicals millions of times
    # (observed 2026-08-09: a 1,675-child transcript pinned a worker for 27+
    # minutes inside this module); the cache turns those into dict hits.
    return " ".join(_WORD_RE.findall(value.casefold().replace("_", " ")))


def _contains_phrase(container: str, phrase: str) -> bool:
    left = _normalize(container).split()
    right = _normalize(phrase).split()
    if not left or not right or len(right) > len(left):
        return False
    return any(left[index:index + len(right)] == right for index in range(len(left) - len(right) + 1))


def _occurrences(text: str, surface: str) -> list[tuple[int, int]]:
    if not surface:
        return []
    left = r"(?<!\w)" if surface[0].isalnum() else ""
    right = r"(?!\w)" if surface[-1].isalnum() else ""
    return [
        (match.start(), match.end())
        for match in re.finditer(left + re.escape(surface) + right, text, re.I)
    ]


_FUNCTION_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "with", "by", "at", "on",
    "in", "to", "from", "as", "but", "not",
})
_LEADING_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.I)
_RELATIVE_LEAD_RE = re.compile(r"^(?:whose|which|who|that|where|when)\b", re.I)
_CLAUSAL_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|has|have|uses|depends|supports|owns|causes|"
    r"produces|consumes|references?|contains?)\b",
)
_Pair = tuple[CompletedMentionV1, DocumentEntityV1]


@dataclass(frozen=True)
class _DocMentionIndex:
    """Per-document precomputation for the ladder.

    The ladder's semantics are defined over `eligible` in MENTION INPUT
    ORDER; every structure here preserves that order so results are
    bit-identical to the original per-call scans. Built once per document
    instead of once per (proposition, role) — the O(P×M) rebuild was the
    hot spot that wedged workers on transcript-scale documents.
    """

    eligible: tuple[_Pair, ...]                 # filtered pairs, input order
    sorted_starts: tuple[int, ...]              # ascending mention starts
    sorted_to_orig: tuple[int, ...]             # sorted position → eligible idx
    by_entity: dict[str, tuple[int, ...]]       # entity_id → eligible idxs (input order)


def _build_doc_index(
    mentions: Sequence[CompletedMentionV1],
    entity_by_id: dict[str, DocumentEntityV1],
) -> _DocMentionIndex:
    eligible: list[_Pair] = []
    by_entity: dict[str, list[int]] = {}
    for mention in mentions:
        entity = entity_by_id.get(mention.entity_id)
        if entity is None or entity.state not in _ELIGIBLE_STATES:
            continue
        # A bare function word is not an entity reference, whatever the
        # census minted — the ladder never links to one.
        if _normalize(entity.canonical_name) in _FUNCTION_WORDS:
            continue
        by_entity.setdefault(entity.entity_id, []).append(len(eligible))
        eligible.append((mention, entity))
    order = sorted(range(len(eligible)), key=lambda i: eligible[i][0].normalized_start)
    return _DocMentionIndex(
        eligible=tuple(eligible),
        sorted_starts=tuple(eligible[i][0].normalized_start for i in order),
        sorted_to_orig=tuple(order),
        by_entity={k: tuple(v) for k, v in by_entity.items()},
    )


def _representative(candidates: list[_Pair]) -> _Pair:
    return sorted(candidates, key=lambda item: (
        -(item[0].normalized_end - item[0].normalized_start),
        item[0].normalized_start,
        item[0].mention_id,
    ))[0]


def _unique_entity(candidates: list[_Pair]) -> _Pair | None:
    """Exactly one ENTITY may survive a rung; ambiguity fails the rung."""
    if not candidates:
        return None
    if len({entity.entity_id for _mention, entity in candidates}) != 1:
        return None
    return _representative(candidates)


def _scope_to_argument_occurrence(
    proposition: OpenIERawPropositionV1,
    role: str,
    surface: str,
    candidates: list[_Pair],
) -> list[_Pair]:
    """Narrow exact-match candidates to the argument's actual occurrence."""
    argument_occurrences = _occurrences(proposition.evidence_text, surface)
    if len(argument_occurrences) > 1:
        relation_start = proposition.evidence_text.casefold().find(proposition.relation.casefold())
        relation_end = relation_start + len(proposition.relation) if relation_start >= 0 else -1
        scoped_occurrences = [
            span for span in argument_occurrences
            if (role == "subject" and span[1] <= relation_start)
            or (role == "object" and relation_end >= 0 and span[0] >= relation_end)
        ]
        if len(scoped_occurrences) == 1:
            argument_occurrences = scoped_occurrences
    if len(argument_occurrences) == 1:
        local_start, local_end = argument_occurrences[0]
        absolute = (
            proposition.evidence_start + local_start,
            proposition.evidence_start + local_end,
        )
        scoped = [
            item for item in candidates
            if absolute[0] <= item[0].normalized_start and item[0].normalized_end <= absolute[1]
        ]
        if scoped:
            return scoped
    return candidates


def _entity_argument(
    proposition: OpenIERawPropositionV1,
    role: str,
    surface: str,
    index: _DocMentionIndex,
) -> AdaptedOpenIEArgumentV1 | None:
    norm_arg = _normalize(surface)
    if not norm_arg or norm_arg in _PRONOUNS or norm_arg in _GENERIC:
        return None

    def build(mention: CompletedMentionV1, entity: DocumentEntityV1, reason: str) -> AdaptedOpenIEArgumentV1:
        return AdaptedOpenIEArgumentV1(
            argument_id=stable_id(
                "adapted-argument", proposition.proposition_id, role, surface,
                OpenIEArgumentKind.ENTITY.value, mention.mention_id, ARGUMENT_ADAPTER_RELEASE,
            ),
            proposition_id=proposition.proposition_id,
            document_id=proposition.document_id,
            unit_id=proposition.unit_id,
            role=role,
            surface=surface,
            kind=OpenIEArgumentKind.ENTITY,
            normalized_start=mention.normalized_start,
            normalized_end=mention.normalized_end,
            mention_id=mention.mention_id,
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            entity_state=entity.state,
            normalized_value=entity.canonical_name,
            reasons=(reason,),
            adapter_release=ARGUMENT_ADAPTER_RELEASE,
        )

    # Window the precomputed eligible pairs to the evidence span via bisect,
    # then restore MENTION INPUT ORDER — identical list to the original
    # per-call scan, without the O(M) walk per (proposition, role).
    local_indices: list[int] = []
    position = bisect_left(index.sorted_starts, proposition.evidence_start)
    while position < len(index.sorted_starts):
        start = index.sorted_starts[position]
        if start > proposition.evidence_end:
            break  # starts are ascending; ends can't fit either (end >= start)
        orig = index.sorted_to_orig[position]
        if index.eligible[orig][0].normalized_end <= proposition.evidence_end:
            insort(local_indices, orig)
        position += 1
    local: list[_Pair] = [index.eligible[i] for i in local_indices]
    local_index_set = frozenset(local_indices)

    # Rung 1: exact completed mention surface.
    exact = [item for item in local if _normalize(item[0].surface) == norm_arg]
    hit = _unique_entity(_scope_to_argument_occurrence(proposition, role, surface, exact))
    if hit:
        return build(*hit, "exact_completed_mention")

    # Rung 2: exact canonical name / explicit alias.
    named = [
        item for item in local
        if _normalize(item[1].canonical_name) == norm_arg
        or any(_normalize(alias) == norm_arg for alias in item[1].aliases)
    ]
    hit = _unique_entity(named)
    if hit:
        return build(*hit, "exact_canonical_or_alias")

    # Containment rungs never fire on clausal or relative-lead arguments — a
    # clause mentioning an entity does not refer to it.
    clausal = (
        _RELATIVE_LEAD_RE.search(surface.strip())
        or _EMBEDDED_RE.search(surface)
        or _CLAUSAL_VERB_RE.search(norm_arg)
        or len(norm_arg.split()) > 8
    )
    if not clausal:
        # Rung 3: unique completed mention contained inside the argument.
        contained = [
            item for item in local
            if _normalize(item[0].surface)
            and _normalize(item[0].surface) not in _GENERIC
            and _normalize(item[0].surface) not in _PRONOUNS
            and _contains_phrase(surface, item[0].surface)
        ]
        hit = _unique_entity(contained)
        if hit:
            return build(*hit, "unique_completed_mention_contained_in_argument")

        # Rung 4: unique canonical/alias phrase contained inside the argument.
        phrase_hits = []
        for item in local:
            for phrase in (item[1].canonical_name, *item[1].aliases):
                normalized_phrase = _normalize(phrase)
                if (
                    normalized_phrase
                    and normalized_phrase not in _GENERIC
                    and _contains_phrase(surface, phrase)
                ):
                    phrase_hits.append(item)
                    break
        hit = _unique_entity(phrase_hits)
        if hit:
            return build(*hit, "unique_canonical_phrase_contained_in_argument")

        # Rung 5: explicit document variant — the argument (article stripped,
        # name-cased) is a contiguous token short form of exactly one document
        # entity's multi-token canonical name ("Solano" ⊂ "Mira Solano").
        bare = _LEADING_ARTICLE_RE.sub("", surface.strip())
        first_alpha = next((char for char in bare if char.isalpha()), "")
        if len(norm_arg) >= 4 and first_alpha.isupper():
            arg_tokens = _normalize(bare).split()
            variant_hits = []
            for pair_indices in index.by_entity.values():
                pairs = [index.eligible[i] for i in pair_indices]
                tokens = _normalize(pairs[0][1].canonical_name).split()
                if len(tokens) >= 2 and 1 <= len(arg_tokens) < len(tokens) and any(
                    tokens[token_index:token_index + len(arg_tokens)] == arg_tokens
                    for token_index in range(len(tokens) - len(arg_tokens) + 1)
                ):
                    in_evidence = [
                        index.eligible[i]
                        for i in pair_indices
                        if i in local_index_set
                    ]
                    variant_hits.append(_representative(in_evidence or pairs))
            hit = _unique_entity(variant_hits)
            if hit:
                return build(*hit, "explicit_document_variant_short_form")
    return None


def _non_entity_argument(
    proposition: OpenIERawPropositionV1,
    role: str,
    surface: str,
) -> AdaptedOpenIEArgumentV1:
    normalized = _normalize(surface)
    local_occurrences = _occurrences(proposition.evidence_text, surface)
    start = end = None
    if len(local_occurrences) == 1:
        start = proposition.evidence_start + local_occurrences[0][0]
        end = proposition.evidence_start + local_occurrences[0][1]
    literal_type = ""
    normalized_value = normalized
    if normalized in _PRONOUNS or normalized in _GENERIC or not normalized:
        kind = OpenIEArgumentKind.UNRESOLVED
        reasons = ("generic_or_pronominal_argument",)
        start = end = None
    elif _DATE_RE.fullmatch(surface.strip()):
        kind = OpenIEArgumentKind.LITERAL
        literal_type = "temporal_or_metric"
        reasons = ("deterministic_literal_shape",)
    elif _EMBEDDED_RE.search(surface) or (len(normalized.split()) >= 6 and re.search(r"\b(?:is|are|was|were|has|have|uses|depends|supports|owns|causes)\b", normalized)):
        kind = OpenIEArgumentKind.EMBEDDED_CLAUSE
        reasons = ("clausal_argument_shape",)
    elif len(normalized.split()) <= 16:
        kind = OpenIEArgumentKind.DESCRIPTION
        reasons = ("bounded_unlinked_noun_phrase",)
    else:
        kind = OpenIEArgumentKind.UNRESOLVED
        reasons = ("unbounded_or_unaligned_argument",)
        start = end = None
    return AdaptedOpenIEArgumentV1(
        argument_id=stable_id(
            "adapted-argument", proposition.proposition_id, role, surface,
            kind.value, start, end, ARGUMENT_ADAPTER_RELEASE,
        ),
        proposition_id=proposition.proposition_id,
        document_id=proposition.document_id,
        unit_id=proposition.unit_id,
        role=role,
        surface=surface,
        kind=kind,
        normalized_start=start,
        normalized_end=end,
        literal_type=literal_type,
        normalized_value=normalized_value,
        reasons=reasons,
        adapter_release=ARGUMENT_ADAPTER_RELEASE,
    )


def adapt_openie_arguments(
    propositions: Sequence[OpenIERawPropositionV1],
    mentions: Sequence[CompletedMentionV1],
    entities: Sequence[DocumentEntityV1],
) -> ArgumentAdapterOutput:
    entity_by_id = {entity.entity_id: entity for entity in entities}
    mentions_by_document: dict[str, list[CompletedMentionV1]] = {}
    for mention in mentions:
        mentions_by_document.setdefault(mention.document_id, []).append(mention)
    index_by_document: dict[str, _DocMentionIndex] = {}
    arguments: list[AdaptedOpenIEArgumentV1] = []
    for proposition in propositions:
        doc_index = index_by_document.get(proposition.document_id)
        if doc_index is None:
            doc_index = _build_doc_index(
                mentions_by_document.get(proposition.document_id, []), entity_by_id,
            )
            index_by_document[proposition.document_id] = doc_index
        for role, surface in (("subject", proposition.subject), ("object", proposition.object)):
            adapted = _entity_argument(proposition, role, surface, doc_index)
            arguments.append(adapted or _non_entity_argument(proposition, role, surface))
    arguments.sort(key=lambda item: (item.proposition_id, item.role, item.argument_id))
    counts = Counter(item.kind.value for item in arguments)
    entity_arguments = [item for item in arguments if item.kind == OpenIEArgumentKind.ENTITY]
    report: dict[str, object] = {
        "schema_version": "polymath.openie_argument_adapter_report.v1",
        "status": "passed",
        "raw_propositions": len(propositions),
        "input_arguments": len(propositions) * 2,
        "classified_arguments": len(arguments),
        "classification_conservation": len(arguments) == len(propositions) * 2,
        "kind_counts": dict(sorted(counts.items())),
        "entity_arguments": len(entity_arguments),
        "alignment_ladder_counts": dict(sorted(Counter(
            item.reasons[0] for item in entity_arguments if item.reasons
        ).items())),
        "strict_entity_alignment_rate": (
            sum(item.normalized_start is not None and item.normalized_end is not None for item in entity_arguments)
            / len(entity_arguments) if entity_arguments else 1.0
        ),
        "accepted_pronoun_entities": sum(
            item.kind == OpenIEArgumentKind.ENTITY and _normalize(item.surface) in _PRONOUNS
            for item in arguments
        ),
        "identity_digest": stable_digest([
            item.model_dump(mode="json") for item in arguments
        ]),
    }
    return ArgumentAdapterOutput(tuple(arguments), report)
