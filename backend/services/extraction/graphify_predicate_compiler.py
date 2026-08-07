"""Compile reduced OpenIE surface relations without inventing ontology edges."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence

from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1,
    OpenIEArgumentKind,
    OpenIEPredicateCandidateV1,
    OpenIEPropositionFamilyV1,
    OpenIERawPropositionV1,
    stable_digest,
    stable_id,
)
from services.extraction.graphify_relations import (
    PREDICATE_COMPILER_RELEASE,
    _CANONICAL_BY_LEMMA,
    predicate_compiler,
)

OPENIE_PREDICATE_COMPILER_RELEASE = "graphify-openie-predicate-compiler-v4"
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_PASSIVE_BY_RE = re.compile(r"\b(?:was|were|is|are|been|be)?\s*([\w-]+(?:ed|en))\s+by\b|\bby$", re.I)
_PASSIVE_ACTIVE_PREDICATES = frozenset({
    "uses", "implements", "owns", "defines", "supports", "produces",
    "consumes", "causes", "stores", "detects", "references",
})


@dataclass(frozen=True)
class PredicateCompilationOutput:
    candidates: tuple[OpenIEPredicateCandidateV1, ...]
    report: dict[str, object]


def _normalize(value: str) -> str:
    return " ".join(_WORD_RE.findall(value.casefold().replace("_", " ")))


def _lemma_candidates(family: OpenIEPropositionFamilyV1, surface: str) -> tuple[str, ...]:
    normalized = _normalize(surface)
    family_lemma = _normalize(family.relation_lemma)
    words = [*family_lemma.split(), *normalized.split()]
    variants: list[str] = [family_lemma, normalized]
    for word in words:
        variants.append(word)
        if word.endswith("ies") and len(word) > 4:
            variants.append(word[:-3] + "y")
        elif word.endswith("es") and len(word) > 4:
            variants.extend((word[:-1], word[:-2]))
        elif word.endswith("s") and len(word) > 3:
            variants.append(word[:-1])
        if word.endswith("ed") and len(word) > 4:
            variants.extend((word[:-2], word[:-1]))
    return tuple(dict.fromkeys(item for item in variants if item))


def _canonical_hint(
    family: OpenIEPropositionFamilyV1,
    surface: str,
) -> tuple[str | None, str, str]:
    normalized = _normalize(surface)
    candidates = _lemma_candidates(family, surface)
    if re.search(r"\b(?:built|build|builds)(?:\s+on)?(?:\s+top)?\b", normalized):
        return None, candidates[0], "review:ambiguous_built_on_top_of"
    if "related" in normalized:
        return "related_to", "relate", "explicit_related_surface"
    if re.search(r"\b(?:part|component)\s+of\b", normalized):
        return "part_of", "part", "explicit_part_of_surface"
    if re.fullmatch(r"occur(?:s|red|ring)?\s+(?:in|at|on|near|during)", normalized):
        return "related_to", "relate", "explicit_occurred_in_closed_ontology"
    if re.search(r"\b(?:acquire|acquires|acquired)\b", normalized):
        return "owns", "acquire", "explicit_acquisition_surface"
    for lemma in candidates:
        if lemma in _CANONICAL_BY_LEMMA:
            return _CANONICAL_BY_LEMMA[lemma], lemma, "bounded_lemma_map"
    return None, candidates[0], "configured_synonym_or_abstain"


def compile_openie_predicates(
    families: Sequence[OpenIEPropositionFamilyV1],
    propositions: Sequence[OpenIERawPropositionV1],
    arguments: Sequence[AdaptedOpenIEArgumentV1],
) -> PredicateCompilationOutput:
    proposition_by_id = {item.proposition_id: item for item in propositions}
    arguments_by_proposition: dict[str, dict[str, AdaptedOpenIEArgumentV1]] = defaultdict(dict)
    for argument in arguments:
        arguments_by_proposition[argument.proposition_id][argument.role] = argument
    compiler = predicate_compiler()
    output: list[OpenIEPredicateCandidateV1] = []
    for family in families:
        proposition = proposition_by_id.get(family.representative_proposition_id)
        adapted = arguments_by_proposition.get(family.representative_proposition_id, {})
        if proposition is None or set(adapted) != {"subject", "object"}:
            raise ValueError(f"incomplete representative family {family.family_id}")
        subject = adapted["subject"]
        obj = adapted["object"]
        surface = proposition.relation.strip()
        proposal_source = (
            "strict_surface_recovery"
            if proposition.extractor_release.endswith(":strict_surface_recovery")
            else "triplet_extract"
        )
        hint, lemma, hint_rule = _canonical_hint(family, surface)
        invalid_endpoint = subject.kind in {
            OpenIEArgumentKind.UNRESOLVED, OpenIEArgumentKind.EMBEDDED_CLAUSE,
        } or obj.kind in {OpenIEArgumentKind.UNRESOLVED, OpenIEArgumentKind.EMBEDDED_CLAUSE}
        if invalid_endpoint:
            candidate = None
            mapping_rule = "review:unresolved_or_embedded_endpoint"
            mapping_status = "REVIEW"
        elif hint_rule.startswith("review:"):
            candidate = None
            mapping_rule = hint_rule
            mapping_status = "REVIEW"
        else:
            candidate, mapping_rule = compiler.compile(
                surface=surface,
                lemma=lemma,
                canonical_hint=hint,
                subject_type=subject.entity_type or "concept",
                object_type=obj.entity_type or "concept",
                subject_name=subject.normalized_value or subject.surface,
                object_name=obj.normalized_value or obj.surface,
                source=f"openie:{proposal_source}:{hint_rule}",
            )
            if (
                hint_rule == "explicit_occurred_in_closed_ontology"
                and mapping_rule == "review:forced_related_to_prohibited"
            ):
                candidate = "related_to"
                mapping_rule = (
                    "mapped:openie:declared_closed_ontology:occurred_in:related_to"
                )
            if (
                candidate is not None
                and mapping_rule.startswith("review:endpoint_signature:")
                and subject.kind == OpenIEArgumentKind.ENTITY
                and obj.kind in {OpenIEArgumentKind.LITERAL, OpenIEArgumentKind.DESCRIPTION}
            ):
                mapping_rule = f"mapped:openie:{proposal_source}:value_object:{lemma}"
            if (
                candidate is not None
                and mapping_rule.startswith("review:endpoint_signature:")
                and proposal_source == "strict_surface_recovery"
                and subject.kind == OpenIEArgumentKind.ENTITY
                and obj.kind == OpenIEArgumentKind.ENTITY
            ):
                mapping_rule = (
                    "mapped:openie:strict_surface_recovery:"
                    f"exact_entity_endpoints:{lemma}"
                )
            if candidate is None:
                mapping_status = "STORE_UNMAPPED_SURFACE_RELATION"
            elif mapping_rule.startswith("review:"):
                mapping_status = "REVIEW"
            else:
                mapping_status = "MAPPED"
        direction_rule = "surface_subject_to_object"
        if candidate in _PASSIVE_ACTIVE_PREDICATES and _PASSIVE_BY_RE.search(surface):
            subject, obj = obj, subject
            direction_rule = "passive_by_swap_to_semantic_agent"
        output.append(OpenIEPredicateCandidateV1(
            candidate_id=stable_id(
                "openie-predicate", family.family_id, subject.argument_id,
                obj.argument_id, candidate, mapping_status,
                OPENIE_PREDICATE_COMPILER_RELEASE,
            ),
            family_id=family.family_id,
            document_id=family.document_id,
            unit_id=family.unit_id,
            subject_argument_id=subject.argument_id,
            object_argument_id=obj.argument_id,
            subject_key=(f"entity:{subject.entity_id}" if subject.entity_id else f"{subject.kind.value.casefold()}:{subject.normalized_value}"),
            subject_kind=subject.kind,
            object_key=(f"entity:{obj.entity_id}" if obj.entity_id else f"{obj.kind.value.casefold()}:{obj.normalized_value}"),
            object_kind=obj.kind,
            surface_relation=surface,
            relation_lemma=lemma,
            canonical_predicate=candidate,
            mapping_status=mapping_status,
            mapping_rule=mapping_rule,
            direction_rule=direction_rule,
            polarity=family.polarity,
            modality=family.modality,
            attribution=family.attribution,
            representative_proposition_id=family.representative_proposition_id,
            rendering_ids=family.rendering_ids,
            evidence_start=family.evidence_start,
            evidence_end=family.evidence_end,
            confidence=family.max_confidence,
            compiler_release=OPENIE_PREDICATE_COMPILER_RELEASE,
        ))
    output.sort(key=lambda item: (item.document_id, item.evidence_start, item.candidate_id))
    counts = Counter(item.mapping_status for item in output)
    report: dict[str, object] = {
        "schema_version": "polymath.openie_predicate_compiler_report.v1",
        "status": "passed",
        "proposition_families": len(families),
        "predicate_candidates": len(output),
        "decision_conservation": len(output) == len(families),
        "mapping_status_counts": dict(sorted(counts.items())),
        "forced_related_to_fallbacks": sum(
            item.canonical_predicate == "related_to" and "related" not in item.surface_relation.casefold()
            for item in output
        ),
        "direction_swaps": sum(item.direction_rule != "surface_subject_to_object" for item in output),
        "predicate_compiler_hash": compiler.config_hash,
        "identity_digest": stable_digest([item.model_dump(mode="json") for item in output]),
    }
    return PredicateCompilationOutput(tuple(output), report)
