"""Conservative exact-span adapter for raw OpenIE arguments."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
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

ARGUMENT_ADAPTER_RELEASE = "graphify-openie-argument-adapter-v1"
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


def _normalize(value: str) -> str:
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


def _entity_argument(
    proposition: OpenIERawPropositionV1,
    role: str,
    surface: str,
    mentions: Sequence[CompletedMentionV1],
    entity_by_id: dict[str, DocumentEntityV1],
) -> AdaptedOpenIEArgumentV1 | None:
    candidates = []
    for mention in mentions:
        entity = entity_by_id.get(mention.entity_id)
        if entity is None or entity.state not in _ELIGIBLE_STATES:
            continue
        if not (
            proposition.evidence_start <= mention.normalized_start
            and mention.normalized_end <= proposition.evidence_end
        ):
            continue
        if _normalize(mention.surface) == _normalize(surface):
            candidates.append((mention, entity))
    if not candidates:
        return None
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
            candidates = scoped
    candidates.sort(key=lambda item: (
        -(item[0].normalized_end - item[0].normalized_start),
        item[0].normalized_start,
        item[0].mention_id,
    ))
    entity_ids = {entity.entity_id for _mention, entity in candidates}
    longest = candidates[0][0].normalized_end - candidates[0][0].normalized_start
    best = [
        item for item in candidates
        if item[0].normalized_end - item[0].normalized_start == longest
    ]
    if len(entity_ids) != 1 or len(best) != 1:
        return None
    mention, entity = best[0]
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
        reasons=("exact_promoted_mention_subspan",),
        adapter_release=ARGUMENT_ADAPTER_RELEASE,
    )


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
    arguments: list[AdaptedOpenIEArgumentV1] = []
    for proposition in propositions:
        local_mentions = mentions_by_document.get(proposition.document_id, [])
        for role, surface in (("subject", proposition.subject), ("object", proposition.object)):
            adapted = _entity_argument(
                proposition, role, surface, local_mentions, entity_by_id,
            )
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
