"""Count-only deterministic census for T9.2 frame and motif contracts."""

from __future__ import annotations

from collections import Counter
import json
from typing import Any

from models.claim_record import ClaimArgumentV1, ClaimRecordV1
from models.frame_motif import FrameInstanceCandidateV1, FrameSequenceItemV1
from models.registry_loader import load_all, registry_hashes
from services.ingestion.frame_motif import compile_frame_instance, match_motifs
from services.ingestion.semantic_resolution import resolve_superframe_rule


def _claim(
    predicate: str,
    ordinal: int,
    *,
    conditions: list[str] | None = None,
    same_surfaces: bool = False,
) -> ClaimRecordV1:
    subject = "Shared label" if same_surfaces else f"source {ordinal}"
    target = "Shared label" if same_surfaces else f"target {ordinal}"
    evidence_id = f"evidence:{ordinal}"
    return ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id=f"claim:{ordinal}",
        document_id="doc:t9-2-census",
        child_id="child:t9-2-census",
        proposition_text=f"{subject} relates to {target}",
        canonical_proposition=f"{subject} {predicate.lower()} {target}",
        claim_type="causal",
        predicate_observation_id=f"predicate-observation:{ordinal}",
        predicate_id=f"predicate:{ordinal}",
        predicate_surface="relates",
        predicate_lemma="relate",
        normalized_predicate=predicate,
        typing_status="typed",
        arguments=[
            ClaimArgumentV1(
                role="subject",
                filler_kind="entity_mention",
                filler_ref=f"mention:{ordinal}:source",
                span_observation_id=f"span:{ordinal}:source",
                surface=subject,
                start_char=0,
                end_char=len(subject),
                evidence_sentence_id=evidence_id,
            ),
            ClaimArgumentV1(
                role="object",
                filler_kind="entity_mention",
                filler_ref=f"mention:{ordinal}:target",
                span_observation_id=f"span:{ordinal}:target",
                surface=target,
                start_char=len(subject) + 12,
                end_char=len(subject) + 12 + len(target),
                evidence_sentence_id=evidence_id,
            ),
        ],
        polarity="positive",
        modality="asserted",
        assertion_mode="reported",
        conditions=conditions or [],
        exceptions=[],
        temporal_cues=[],
        evidence_sentence_ids=[evidence_id],
        source_relation_ids=[],
        scope_hash="sha256:t9-2-count-only-fixture",
        knowledge_status="candidate",
        validation_status="candidate",
    )


def _compile(
    claim: ClaimRecordV1,
    source_thread: str,
    target_thread: str,
) -> FrameInstanceCandidateV1:
    resolution = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id={
            claim.arguments[0].filler_ref: "BEHAVIOR",
            claim.arguments[1].filler_ref: "OUTCOME",
        },
    )
    if len(resolution.matches) != 1:
        raise RuntimeError("census claim did not resolve to exactly one frame")
    return compile_frame_instance(
        claim,
        resolution.matches[0],
        thread_keys_by_filler_ref={
            claim.arguments[0].filler_ref: source_thread,
            claim.arguments[1].filler_ref: target_thread,
        },
    )


def _m03(
    offset: int,
    continuity: str,
    *,
    admissible_outcome: bool = False,
    same_surfaces: bool = False,
) -> list[FrameInstanceCandidateV1]:
    predicates = [
        "USED_FOR",
        "REQUIRES",
        "USED_FOR",
        "USED_FOR" if admissible_outcome else "CAUSES",
    ]
    if continuity == "directional":
        sources = [f"thread:{offset}:{index}" for index in range(4)]
        targets = [f"thread:{offset}:{index}" for index in range(1, 5)]
    elif continuity == "shared":
        sources = [f"thread:{offset}:actor"] * 4
        targets = [f"thread:{offset}:target:{index}" for index in range(4)]
    elif continuity == "partial":
        sources = ["a", "b", "x", "y"]
        targets = ["b", "c", "d", "z"]
        sources = [f"thread:{offset}:{item}" for item in sources]
        targets = [f"thread:{offset}:{item}" for item in targets]
    elif continuity == "none":
        sources = [f"thread:{offset}:source:{index}" for index in range(4)]
        targets = [f"thread:{offset}:target:{index}" for index in range(4)]
    else:
        raise ValueError(continuity)
    return [
        _compile(
            _claim(
                predicate,
                offset + index,
                same_surfaces=same_surfaces,
            ),
            sources[index],
            targets[index],
        )
        for index, predicate in enumerate(predicates)
    ]


def _m12(offset: int, *, with_condition: bool) -> list[FrameInstanceCandidateV1]:
    return [
        _compile(
            _claim(
                "CAUSES",
                offset + index,
                conditions=["under congestion"]
                if with_condition and index == 1
                else [],
            ),
            f"thread:{offset}:{index}",
            f"thread:{offset}:{index + 1}",
        )
        for index in range(3)
    ]


def _match(label: str, frames: list[FrameInstanceCandidateV1]):
    return label, match_motifs(
        target_artifact_id=f"parent:{label}",
        frame_instances=frames,
        sequence_items=[
            FrameSequenceItemV1(
                schema_version="frame_sequence_item.v1",
                sequence_index=index,
                frame_instance_id=frame.frame_instance_id,
            )
            for index, frame in enumerate(frames)
        ],
    )


def build_receipt() -> dict[str, Any]:
    hashes = registry_hashes()
    registries = load_all()
    sequences = {
        "m03_directional": _m03(0, "directional"),
        "m03_shared": _m03(10, "shared"),
        "m03_partial": _m03(20, "partial"),
        "m03_disconnected_same_surface": _m03(
            30,
            "none",
            same_surfaces=True,
        ),
        "m03_admissible_outcome": _m03(
            40,
            "directional",
            admissible_outcome=True,
        ),
        "m12_condition_satisfied": _m12(50, with_condition=True),
        "m12_condition_missing": _m12(60, with_condition=False),
    }
    directional = sequences["m03_directional"]
    gap_frame = _compile(
        _claim("SIGNALS", 70),
        "thread:gap:source",
        "thread:gap:target",
    )
    negative_sequences = {
        "m03_missing_stage": directional[:-1],
        "m03_intervening_frame": [directional[0], gap_frame, *directional[1:]],
        "m03_reordered": [directional[1], directional[0], *directional[2:]],
    }
    results = [_match(label, frames) for label, frames in sequences.items()]
    negative_results = [
        _match(label, frames) for label, frames in negative_sequences.items()
    ]
    candidates = [
        candidate
        for _, result in results
        for candidate in result.candidates
    ]
    negative_m03_matches = sum(
        candidate.motif_id == "M03"
        for _, result in negative_results
        for candidate in result.candidates
    )
    all_frames = {
        frame.frame_instance_id: frame
        for frames in [*sequences.values(), [gap_frame]]
        for frame in frames
    }

    dispositions = Counter(item.matcher_disposition for item in candidates)
    transition_classes = Counter(
        transition.classification
        for candidate in candidates
        for transition in candidate.role_transitions
    )
    tier_counts = Counter(
        stage.match_tier
        for candidate in candidates
        for stage in candidate.stage_matches
    )
    frame_counts = Counter(item.frame_id for item in all_frames.values())
    frame_recipe_hashes = sorted(
        {item.compilation_recipe_hash for item in all_frames.values()}
    )
    matcher_recipe_hashes = sorted(
        {result.matcher_recipe_hash for _, result in [*results, *negative_results]}
    )
    receipt = {
        "schema_version": "frame_motif_t9_2_census.v1",
        "authority": "executor-proposed, owner-ratifiable",
        "execution": {
            "mode": "local_deterministic_count_only",
            "provider_calls": 0,
            "durable_writes": 0,
            "spend_usd": 0,
        },
        "registries": {
            "frame_role_binding_policy_hash": hashes["frame_role_binding"],
            "motif_matching_policy_hash": hashes["motif_matching"],
            "predicate_lane_reachable_superframe_count": registries[
                "frame_role_binding"
            ]["coverage"]["predicate_lane_reachable_superframe_count"],
            "total_superframe_count": registries["frame_role_binding"][
                "coverage"
            ]["total_superframe_count"],
            "generic_matcher_supported_motif_count": registries[
                "motif_matching"
            ]["coverage"]["generic_matcher_supported_motif_count"],
            "predicate_lane_reachable_motif_count": registries[
                "motif_matching"
            ]["coverage"]["predicate_lane_reachable_motif_count"],
            "predicate_lane_reachable_motif_ids": registries[
                "motif_matching"
            ]["coverage"]["predicate_lane_reachable_motif_ids"],
            "total_motif_count": registries["motif_matching"]["coverage"][
                "total_motif_count"
            ],
        },
        "frame_compilation": {
            "frame_instance_count": len(all_frames),
            "frame_counts": dict(sorted(frame_counts.items())),
            "role_binding_count": sum(
                len(item.role_bindings) for item in all_frames.values()
            ),
            "source_binding_count": sum(
                binding.relation_direction_role == "source"
                for item in all_frames.values()
                for binding in item.role_bindings
            ),
            "target_binding_count": sum(
                binding.relation_direction_role == "target"
                for item in all_frames.values()
                for binding in item.role_bindings
            ),
            "unbound_argument_count": sum(
                item.unbound_argument_count for item in all_frames.values()
            ),
            "unbound_count_is_definitional": True,
            "candidate_state_count": sum(
                item.assignment_state == "candidate" for item in all_frames.values()
            ),
            "accepted_state_count": sum(
                item.accepted_state_written for item in all_frames.values()
            ),
            "compilation_recipe_hashes": frame_recipe_hashes,
        },
        "motif_matching": {
            "positive_case_count": len(results),
            "negative_case_count": len(negative_results),
            "sequence_aligned_window_count": len(candidates),
            "dispositions": dict(sorted(dispositions.items())),
            "transition_classifications": dict(sorted(transition_classes.items())),
            "stage_match_tiers": dict(sorted(tier_counts.items())),
            "sequence_alignment_values": sorted(
                {item.sequence_alignment for item in candidates}
            ),
            "sequence_alignment_is_definitional": True,
            "role_continuity_values": sorted(
                {item.role_continuity for item in candidates}
            ),
            "m12_required_condition_missing_count": sum(
                "required_condition_missing" in item.rejection_reasons
                for item in candidates
            ),
            "strict_negative_m03_match_count": negative_m03_matches,
            "accepted_state_count": sum(
                item.accepted_state_written for item in candidates
            ),
            "fused_final_score_present": any(
                "final_score" in item.model_dump() for item in candidates
            ),
            "matcher_recipe_hashes": matcher_recipe_hashes,
        },
    }
    expected = {
        "frame_instance_count": 27,
        "role_binding_count": 54,
        "source_binding_count": 27,
        "target_binding_count": 27,
        "unbound_argument_count": 0,
        "candidate_state_count": 27,
        "accepted_state_count": 0,
    }
    for key, value in expected.items():
        if receipt["frame_compilation"][key] != value:
            raise RuntimeError(f"T9.2 frame census invariant failed: {key}")
    matching_expected = {
        "sequence_aligned_window_count": 7,
        "dispositions": {
            "confirmed_candidate": 4,
            "provisional": 1,
            "rejected": 2,
        },
        "transition_classifications": {
            "directional": 11,
            "disconnected": 5,
            "shared_participant": 3,
        },
        "stage_match_tiers": {"admissible": 1, "dominant": 25},
        "sequence_alignment_values": [1.0],
        "m12_required_condition_missing_count": 1,
        "strict_negative_m03_match_count": 0,
        "accepted_state_count": 0,
        "fused_final_score_present": False,
    }
    for key, value in matching_expected.items():
        if receipt["motif_matching"][key] != value:
            raise RuntimeError(f"T9.2 motif census invariant failed: {key}")
    if receipt["registries"]["predicate_lane_reachable_superframe_count"] != 8:
        raise RuntimeError("T9.2 8/16 frame coverage drifted")
    if receipt["registries"]["predicate_lane_reachable_motif_count"] != 4:
        raise RuntimeError("T9.2 4/12 motif coverage drifted")
    return receipt


if __name__ == "__main__":
    print(json.dumps(build_receipt(), indent=2, sort_keys=True))
