"""Count-only deterministic census for the T9.1 resolution boundary.

The harness uses synthetic identifiers and controlled labels only. It makes no
provider calls and writes no data; the canonical JSON receipt is printed to
stdout so the caller can capture a true-exit-code log.
"""

from __future__ import annotations

from collections import Counter
import json
from typing import Any

from models.claim_record import ClaimArgumentV1, ClaimRecordV1
from models.registry_loader import load_all, registry_hashes
from models.semantic_resolution import DomainSignalV1
from services.ingestion.corpus_lexicon import normalize_identity
from services.ingestion.semantic_resolution import (
    build_domain_affinity_serve_view,
    resolve_domains,
    resolve_superframe_rule,
)


def _signal(signal_id: str, label: str, signal_kind: str) -> DomainSignalV1:
    return DomainSignalV1(
        schema_version="domain_signal.v1",
        signal_id=signal_id,
        label=label,
        signal_kind=signal_kind,
        evidence_ref_ids=[f"evidence:{signal_id}"],
        supporting_claim_ids=(
            [f"claim:{signal_id}"] if signal_kind == "claim_concept" else []
        ),
    )


def _claim(
    predicate: str | None,
    *,
    claim_id: str,
    subject: str = "A process",
) -> ClaimRecordV1:
    typed = predicate is not None
    return ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id=claim_id,
        document_id="doc:t9-1-census",
        child_id="child:t9-1-census",
        proposition_text=f"{subject} changes the baseline",
        canonical_proposition=f"{subject.lower()} change baseline",
        claim_type="causal" if typed else "description_or_observation",
        predicate_observation_id=f"predicate-observation:{claim_id}",
        predicate_id=f"predicate:{claim_id}" if typed else None,
        predicate_surface="changes",
        predicate_lemma="change",
        normalized_predicate=predicate,
        typing_status="typed" if typed else "untyped",
        arguments=[
            ClaimArgumentV1(
                role="subject",
                filler_kind="entity_mention",
                filler_ref=f"mention:{claim_id}:subject",
                span_observation_id=f"span:{claim_id}:subject",
                surface=subject,
                start_char=0,
                end_char=len(subject),
                evidence_sentence_id=f"evidence:{claim_id}",
            ),
            ClaimArgumentV1(
                role="object",
                filler_kind="entity_mention",
                filler_ref=f"mention:{claim_id}:object",
                span_observation_id=f"span:{claim_id}:object",
                surface="the baseline",
                start_char=len(subject) + 9,
                end_char=len(subject) + 21,
                evidence_sentence_id=f"evidence:{claim_id}",
            ),
        ],
        polarity="positive",
        modality="asserted",
        assertion_mode="reported",
        conditions=[],
        exceptions=[],
        temporal_cues=[],
        evidence_sentence_ids=[f"evidence:{claim_id}"],
        source_relation_ids=[],
        scope_hash="sha256:t9-1-count-only-fixture",
        knowledge_status="candidate",
        validation_status="candidate",
    )


def _entity_types(claim: ClaimRecordV1, object_type: str) -> dict[str, str]:
    return {
        item.filler_ref: "BEHAVIOR" if item.role == "subject" else object_type
        for item in claim.arguments
        if item.filler_kind == "entity_mention"
    }


def build_receipt() -> dict[str, Any]:
    registries = load_all()
    hashes = registry_hashes()
    domain_signals = [
        _signal("domain-1", "Data Science", "claim_concept"),
        _signal(
            "domain-2",
            "Technology and Engineered Systems",
            "section_heading",
        ),
        _signal("domain-3", "Markets", "section_heading"),
        _signal("domain-4", "Data Sciences", "section_heading"),
        _signal("domain-5", "Market", "section_heading"),
    ]
    domain_first = resolve_domains(
        target_artifact_id="parent:t9-1-census",
        signals=domain_signals,
    )
    domain_second = resolve_domains(
        target_artifact_id="parent:t9-1-census",
        signals=domain_signals,
    )
    affinity_view = build_domain_affinity_serve_view(domain_first)

    frame_resolutions = []
    for predicate in registries["vocab"]["predicate_types"]:
        claim = _claim(predicate, claim_id=f"claim:{predicate.lower()}")
        frame_resolutions.append(
            resolve_superframe_rule(
                claim,
                entity_types_by_mention_id=_entity_types(claim, "QUALITY"),
            )
        )
    specialized_claim = _claim(
        "DECREASES",
        claim_id="claim:decreases-specialized",
        subject="Repeated discounting",
    )
    frame_resolutions.append(
        resolve_superframe_rule(
            specialized_claim,
            entity_types_by_mention_id=_entity_types(specialized_claim, "BASELINE"),
        )
    )
    untyped_claim = _claim(None, claim_id="claim:untyped")
    frame_resolutions.append(
        resolve_superframe_rule(
            untyped_claim,
            entity_types_by_mention_id={},
        )
    )
    frame_replay = []
    for resolution in frame_resolutions[:-1]:
        predicate = resolution.predicate_type
        claim_id = resolution.target_claim_id
        if claim_id == "claim:decreases-specialized":
            replay_claim = specialized_claim
            object_type = "BASELINE"
        else:
            replay_claim = _claim(predicate, claim_id=claim_id)
            object_type = "QUALITY"
        frame_replay.append(
            resolve_superframe_rule(
                replay_claim,
                entity_types_by_mention_id=_entity_types(replay_claim, object_type),
            )
        )
    frame_replay.append(
        resolve_superframe_rule(untyped_claim, entity_types_by_mention_id={})
    )

    frame_counts = Counter(
        match.frame_id
        for resolution in frame_resolutions
        for match in resolution.matches
    )
    abstention_counts = Counter(
        resolution.explicit_abstention_reason
        for resolution in frame_resolutions
        if resolution.explicit_abstention_reason is not None
    )
    domain_terms: dict[str, str] = {}
    collision_count = 0
    for row in registries["domain"]["domains"]:
        for surface in [row["name"], *row["members"]]:
            normalized = normalize_identity(surface)
            if (
                normalized in domain_terms
                and domain_terms[normalized] != row["domain_id"]
            ):
                collision_count += 1
            domain_terms[normalized] = row["domain_id"]

    receipt = {
        "schema_version": "semantic_resolution_t9_1_census.v1",
        "authority": "executor-proposed, owner-ratifiable",
        "execution": {
            "mode": "local_deterministic_count_only",
            "provider_calls": 0,
            "durable_writes": 0,
            "spend_usd": 0,
        },
        "registries": {
            "domain_resolution_hash": hashes["domain_resolution"],
            "superframe_rule_hash": hashes["superframe_rule"],
            "domain_owner_term_count": len(domain_terms),
            "domain_owner_term_collision_count": collision_count,
            "controlled_predicate_count": len(
                registries["vocab"]["predicate_types"]
            ),
            "predicate_route_reachable_superframe_count": registries[
                "superframe_rule"
            ]["coverage"]["reachable_superframe_count"],
            "total_superframe_count": registries["superframe_rule"]["coverage"][
                "total_superframe_count"
            ],
        },
        "domain_resolution": domain_first.receipt(),
        "affinity_quarantine": {
            "prior_row_count": len(affinity_view.priors),
            "serve_only": affinity_view.serve_only,
            "excluded_from_semantic_identity": (
                affinity_view.excluded_from_semantic_identity
            ),
            "excluded_from_acceptance": affinity_view.excluded_from_acceptance,
        },
        "superframe_resolution": {
            "resolution_count": len(frame_resolutions),
            "candidate_match_count": sum(
                len(item.matches) for item in frame_resolutions
            ),
            "assignment_states": sorted(
                {
                    match.assignment_state
                    for item in frame_resolutions
                    for match in item.matches
                }
            ),
            "frame_counts": dict(sorted(frame_counts.items())),
            "distinct_frame_count": len(frame_counts),
            "abstention_counts": dict(sorted(abstention_counts.items())),
            "owner_attention_count": sum(
                match.owner_attention
                for item in frame_resolutions
                for match in item.matches
            ),
            "mf15_specialization_count": frame_counts["MF15"],
        },
        "invariants": {
            "domain_replay_byte_identical": (
                domain_first.model_dump_json() == domain_second.model_dump_json()
            ),
            "frame_replay_byte_identical": all(
                first.model_dump_json() == second.model_dump_json()
                for first, second in zip(frame_resolutions, frame_replay)
            ),
            "predicate_is_domain_bearing": registries["domain_resolution"][
                "predicate_policy"
            ]["domain_bearing"],
            "scalar_score_present": registries["domain_resolution"][
                "scalar_score"
            ]
            is not None,
            "affinity_enters_identity": (
                not affinity_view.excluded_from_semantic_identity
            ),
            "accepted_state_count": 0,
        },
    }
    expected = {
        "candidate_match_count": 17,
        "distinct_frame_count": 8,
        "mf15_specialization_count": 1,
        "owner_attention_count": 1,
    }
    for key, value in expected.items():
        if receipt["superframe_resolution"][key] != value:
            raise RuntimeError(f"T9.1 census invariant failed: {key}")
    if receipt["registries"]["domain_owner_term_count"] != 162:
        raise RuntimeError("T9.1 domain registry term count drifted")
    if receipt["registries"]["domain_owner_term_collision_count"] != 0:
        raise RuntimeError("T9.1 domain registry collision detected")
    if not all(
        (
            receipt["invariants"]["domain_replay_byte_identical"],
            receipt["invariants"]["frame_replay_byte_identical"],
            not receipt["invariants"]["predicate_is_domain_bearing"],
            not receipt["invariants"]["scalar_score_present"],
            not receipt["invariants"]["affinity_enters_identity"],
        )
    ):
        raise RuntimeError("T9.1 resolver invariants failed")
    return receipt


if __name__ == "__main__":
    print(json.dumps(build_receipt(), indent=2, sort_keys=True))
