"""Assemble evidence-scoped OpenIE candidates into explicit authority lanes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Sequence

from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1,
    OpenIEArgumentKind,
    OpenIEAssertionV1,
    OpenIEPredicateCandidateV1,
    stable_digest,
    stable_id,
)

OPENIE_ASSERTION_POLICY_RELEASE = "graphify-openie-assertion-policy-v2"


@dataclass(frozen=True)
class OpenIEAssertionOutput:
    assertions: tuple[OpenIEAssertionV1, ...]
    report: dict[str, object]


def _invalid_endpoint_surface(value: str) -> bool:
    stripped = value.strip()
    return not stripped or re.search(r"[A-Za-z0-9]", stripped) is None


def assemble_openie_assertions(
    candidates: Sequence[OpenIEPredicateCandidateV1],
    arguments: Sequence[AdaptedOpenIEArgumentV1],
) -> OpenIEAssertionOutput:
    argument_by_id = {item.argument_id: item for item in arguments}
    assertions: list[OpenIEAssertionV1] = []
    invalid_endpoint_flags: list[bool] = []
    for candidate in candidates:
        subject = argument_by_id.get(candidate.subject_argument_id)
        obj = argument_by_id.get(candidate.object_argument_id)
        if subject is None or obj is None:
            raise ValueError(f"candidate references missing argument {candidate.candidate_id}")
        qualified = (
            candidate.polarity != "positive"
            or candidate.modality != "asserted"
            or candidate.attribution != "direct"
        )
        invalid_endpoint = (
            _invalid_endpoint_surface(subject.surface)
            or _invalid_endpoint_surface(obj.surface)
        )
        invalid_endpoint_flags.append(invalid_endpoint)
        reasons = [candidate.mapping_rule, candidate.direction_rule]
        if invalid_endpoint:
            lane = "REJECT"
            reasons.append("invalid_endpoint_sentinel")
        elif qualified:
            lane = "QUALIFIED_CLAIM"
            reasons.append("non_positive_modal_or_attributed")
        elif candidate.mapping_status == "STORE_UNMAPPED_SURFACE_RELATION":
            lane = "OPEN_RELATION"
            reasons.append("surface_relation_preserved_without_ontology_fallback")
        elif candidate.mapping_status == "REVIEW":
            lane = "REVIEW"
            reasons.append("predicate_or_endpoint_requires_review")
        elif subject.kind != OpenIEArgumentKind.ENTITY:
            lane = "REJECT"
            reasons.append("fact_subject_is_not_a_canonical_entity")
        elif obj.kind in {OpenIEArgumentKind.UNRESOLVED, OpenIEArgumentKind.EMBEDDED_CLAUSE}:
            lane = "REJECT"
            reasons.append("unresolved_or_embedded_object")
        elif subject.entity_state is None or obj.kind == OpenIEArgumentKind.ENTITY and obj.entity_state is None:
            lane = "REVIEW"
            reasons.append("missing_entity_terminal_state")
        elif (
            obj.kind in {OpenIEArgumentKind.LITERAL, OpenIEArgumentKind.DESCRIPTION}
            and "strict_surface_recovery" not in candidate.mapping_rule
        ):
            lane = "REVIEW"
            reasons.append("value_fact_requires_strict_surface_recovery")
        else:
            lane = "FACT"
            reasons.append("mapped_positive_asserted_exact_evidence")
        value_kind = obj.kind if obj.kind in {OpenIEArgumentKind.LITERAL, OpenIEArgumentKind.DESCRIPTION} else None
        assertions.append(OpenIEAssertionV1(
            assertion_id=stable_id(
                "openie-assertion", candidate.candidate_id, lane,
                candidate.canonical_predicate, OPENIE_ASSERTION_POLICY_RELEASE,
            ),
            candidate_id=candidate.candidate_id,
            family_id=candidate.family_id,
            document_id=candidate.document_id,
            lane=lane,
            canonical_predicate=candidate.canonical_predicate,
            surface_relation=candidate.surface_relation,
            subject_argument_id=subject.argument_id,
            object_argument_id=obj.argument_id,
            subject_entity_id=subject.entity_id,
            object_entity_id=obj.entity_id,
            subject_mention_id=subject.mention_id,
            object_mention_id=obj.mention_id,
            value_kind=value_kind,
            value=(obj.normalized_value or obj.surface) if value_kind else "",
            polarity=candidate.polarity,
            modality=candidate.modality,
            attribution=candidate.attribution,
            evidence_start=candidate.evidence_start,
            evidence_end=candidate.evidence_end,
            score=candidate.confidence,
            reasons=tuple(reasons),
            policy_release=OPENIE_ASSERTION_POLICY_RELEASE,
        ))
    assertions.sort(key=lambda item: (item.document_id, item.evidence_start, item.assertion_id))
    counts = Counter(item.lane for item in assertions)
    report: dict[str, object] = {
        "schema_version": "polymath.openie_assertion_report.v1",
        "status": "passed",
        "predicate_candidates": len(candidates),
        "assertion_decisions": len(assertions),
        "decision_conservation": len(candidates) == len(assertions),
        "lane_counts": dict(sorted(counts.items())),
        "fact_entity_edges": sum(
            item.lane == "FACT" and item.subject_entity_id is not None and item.object_entity_id is not None
            for item in assertions
        ),
        "fact_values": sum(item.lane == "FACT" and item.value_kind is not None for item in assertions),
        "wildcard_endpoints": sum(
            invalid and item.lane != "REJECT"
            for invalid, item in zip(invalid_endpoint_flags, assertions)
        ),
        "wildcard_non_reject_endpoints": sum(
            invalid and item.lane != "REJECT"
            for invalid, item in zip(invalid_endpoint_flags, assertions)
        ),
        "invalid_endpoint_rejections": sum(
            invalid and item.lane == "REJECT"
            for invalid, item in zip(invalid_endpoint_flags, assertions)
        ),
        "qualified_promoted_to_fact": sum(
            item.lane == "FACT" and (
                item.polarity != "positive" or item.modality != "asserted" or item.attribution != "direct"
            )
            for item in assertions
        ),
        "identity_digest": stable_digest([item.model_dump(mode="json") for item in assertions]),
    }
    return OpenIEAssertionOutput(tuple(assertions), report)
