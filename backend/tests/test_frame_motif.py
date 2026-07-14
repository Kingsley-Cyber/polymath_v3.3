"""T9.2 lossless frame role bindings and strict motif matching."""

from __future__ import annotations

import copy
import inspect

import pytest
from pydantic import ValidationError

from models.claim_record import ClaimArgumentV1, ClaimRecordV1
from models.frame_motif import FrameInstanceCandidateV1, FrameSequenceItemV1
from models.registry_loader import RegistryError, load_all
from services.ingestion import frame_motif
from services.ingestion.frame_motif import compile_frame_instance, match_motifs
from services.ingestion.semantic_resolution import resolve_superframe_rule


def _claim(
    predicate: str,
    ordinal: int,
    *,
    conditions: list[str] | None = None,
    subject_surface: str | None = None,
    object_surface: str | None = None,
) -> ClaimRecordV1:
    subject = subject_surface or f"source {ordinal}"
    target = object_surface or f"target {ordinal}"
    evidence_id = f"evidence:{ordinal}"
    return ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id=f"claim:{ordinal}",
        document_id="doc:t9-2",
        child_id="child:t9-2",
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
        scope_hash="sha256:t9-2-fixture",
        knowledge_status="candidate",
        validation_status="candidate",
    )


def _compile(
    claim: ClaimRecordV1,
    source_thread: str,
    target_thread: str,
) -> FrameInstanceCandidateV1:
    entity_types = {
        claim.arguments[0].filler_ref: "BEHAVIOR",
        claim.arguments[1].filler_ref: "OUTCOME",
    }
    resolution = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id=entity_types,
    )
    assert len(resolution.matches) == 1
    return compile_frame_instance(
        claim,
        resolution.matches[0],
        thread_keys_by_filler_ref={
            claim.arguments[0].filler_ref: source_thread,
            claim.arguments[1].filler_ref: target_thread,
        },
    )


def _m03_frames(
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
        sources = ["thread:0", "thread:1", "thread:2", "thread:3"]
        targets = ["thread:1", "thread:2", "thread:3", "thread:4"]
    elif continuity == "shared":
        sources = ["thread:actor"] * 4
        targets = [f"thread:target:{index}" for index in range(4)]
    elif continuity == "partial":
        sources = ["thread:a", "thread:b", "thread:x", "thread:y"]
        targets = ["thread:b", "thread:c", "thread:d", "thread:z"]
    elif continuity == "none":
        sources = [f"thread:source:{index}" for index in range(4)]
        targets = [f"thread:target:{index}" for index in range(4)]
    else:  # pragma: no cover - fixture guard
        raise ValueError(continuity)
    return [
        _compile(
            _claim(
                predicate,
                index,
                subject_surface="Shared label" if same_surfaces else None,
                object_surface="Shared label" if same_surfaces else None,
            ),
            sources[index],
            targets[index],
        )
        for index, predicate in enumerate(predicates)
    ]


def _match(frames: list[FrameInstanceCandidateV1]):
    return match_motifs(
        target_artifact_id="parent:t9-2",
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


def _candidate(result, motif_id: str):
    rows = [item for item in result.candidates if item.motif_id == motif_id]
    assert len(rows) == 1
    return rows[0]


def test_frame_compiler_binds_every_lawful_argument_losslessly():
    claim = _claim(
        "CAUSES",
        1,
        conditions=["under high load"],
        subject_surface="Repeated retries",
        object_surface="service latency",
    )
    frame = _compile(claim, "concept:retry", "metric:latency")

    assert frame.frame_id == "MF04"
    assert frame.instance_role == "primary"
    assert frame.assignment_state == "candidate"
    assert frame.accepted_state_written is False
    assert frame.unbound_argument_count == 0
    assert frame.unbound_argument_refs == []
    assert [item.claim_argument_role for item in frame.role_bindings] == [
        "subject",
        "object",
    ]
    assert [item.relation_direction_role for item in frame.role_bindings] == [
        "source",
        "target",
    ]
    assert [item.thread_key for item in frame.role_bindings] == [
        "concept:retry",
        "metric:latency",
    ]
    for argument, binding in zip(claim.arguments, frame.role_bindings):
        assert binding.filler_ref == argument.filler_ref
        assert binding.span_observation_id == argument.span_observation_id
        assert binding.evidence_sentence_id == argument.evidence_sentence_id
        assert (binding.start_char, binding.end_char) == (
            argument.start_char,
            argument.end_char,
        )
    assert frame.conditions == claim.conditions
    assert frame.evidence_sentence_ids == claim.evidence_sentence_ids


def test_thread_key_mapping_has_exact_closure_and_no_implicit_coercion():
    claim = _claim("CAUSES", 2)
    resolution = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id={
            claim.arguments[0].filler_ref: "BEHAVIOR",
            claim.arguments[1].filler_ref: "OUTCOME",
        },
    )
    match = resolution.matches[0]
    with pytest.raises(ValueError, match="missing claim filler refs"):
        compile_frame_instance(
            claim,
            match,
            thread_keys_by_filler_ref={claim.arguments[0].filler_ref: "thread:a"},
        )
    with pytest.raises(ValueError, match="unknown claim filler refs"):
        compile_frame_instance(
            claim,
            match,
            thread_keys_by_filler_ref={
                claim.arguments[0].filler_ref: "thread:a",
                claim.arguments[1].filler_ref: "thread:b",
                "mention:ghost": "thread:ghost",
            },
        )
    with pytest.raises(ValueError, match="explicit nonempty strings"):
        compile_frame_instance(
            claim,
            match,
            thread_keys_by_filler_ref={
                claim.arguments[0].filler_ref: "thread:a",
                claim.arguments[1].filler_ref: 42,
            },
        )


def test_claim_role_vocabulary_change_requires_frame_instance_v2():
    claim = _claim("CAUSES", 3)
    resolution = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id={
            claim.arguments[0].filler_ref: "BEHAVIOR",
            claim.arguments[1].filler_ref: "OUTCOME",
        },
    )
    policy = copy.deepcopy(load_all()["frame_role_binding"])
    policy["current_claim_argument_role_vocabulary"].append("instrument")
    with pytest.raises(RegistryError, match="FrameInstance v2 is required"):
        compile_frame_instance(
            claim,
            resolution.matches[0],
            thread_keys_by_filler_ref={
                claim.arguments[0].filler_ref: "thread:a",
                claim.arguments[1].filler_ref: "thread:b",
            },
            frame_role_policy=policy,
        )


def test_same_version_policy_mutation_requires_new_registry_version():
    claim = _claim("CAUSES", 4)
    resolution = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id={
            claim.arguments[0].filler_ref: "BEHAVIOR",
            claim.arguments[1].filler_ref: "OUTCOME",
        },
    )
    frame_policy = copy.deepcopy(load_all()["frame_role_binding"])
    frame_policy["thread_key_policy"]["surface_matching"] = True
    with pytest.raises(RegistryError, match="publish a new registry version"):
        compile_frame_instance(
            claim,
            resolution.matches[0],
            thread_keys_by_filler_ref={
                claim.arguments[0].filler_ref: "thread:a",
                claim.arguments[1].filler_ref: "thread:b",
            },
            frame_role_policy=frame_policy,
        )

    frames = _m03_frames("directional")
    motif_policy = copy.deepcopy(load_all()["motif_matching"])
    motif_policy["sequence_tolerance"]["maximum_missing_stages"] = 1
    with pytest.raises(RegistryError, match="publish a new registry version"):
        match_motifs(
            target_artifact_id="parent:t9-2",
            frame_instances=frames,
            sequence_items=[
                FrameSequenceItemV1(
                    schema_version="frame_sequence_item.v1",
                    sequence_index=index,
                    frame_instance_id=frame.frame_instance_id,
                )
                for index, frame in enumerate(frames)
            ],
            matching_policy=motif_policy,
        )


def test_m03_strict_sequence_and_directional_threading_confirm_candidate():
    result = _match(_m03_frames("directional"))
    candidate = _candidate(result, "M03")

    assert [item.frame_id for item in candidate.stage_matches] == [
        "MF06",
        "MF09",
        "MF06",
        "MF04",
    ]
    assert {item.match_tier for item in candidate.stage_matches} == {"dominant"}
    assert candidate.sequence_alignment == 1.0
    assert candidate.sequence_alignment_interpretation == (
        "definitional_under_strict_v1_not_quality_signal"
    )
    assert candidate.role_continuity == 1.0
    assert [item.classification for item in candidate.role_transitions] == [
        "directional",
        "directional",
        "directional",
    ]
    assert candidate.matcher_disposition == "confirmed_candidate"
    assert candidate.assignment_state == "candidate"
    assert candidate.accepted_state_written is False
    assert "final_score" not in candidate.model_dump()


def test_owner_approved_admissible_binding_matches_and_retains_tier():
    candidate = _candidate(
        _match(_m03_frames("directional", admissible_outcome=True)),
        "M03",
    )
    assert candidate.stage_matches[-1].frame_id == "MF06"
    assert candidate.stage_matches[-1].match_tier == "admissible"
    assert candidate.score_components.dominant_stage_count == 3
    assert candidate.score_components.admissible_stage_count == 1
    assert candidate.sequence_alignment == 1.0


def test_role_continuity_trichotomy_is_threshold_free():
    confirmed = _candidate(_match(_m03_frames("shared")), "M03")
    provisional = _candidate(_match(_m03_frames("partial")), "M03")
    rejected = _candidate(_match(_m03_frames("none")), "M03")

    assert {item.classification for item in confirmed.role_transitions} == {
        "shared_participant"
    }
    assert confirmed.role_continuity == 1.0
    assert confirmed.matcher_disposition == "confirmed_candidate"
    assert provisional.role_continuity == pytest.approx(1 / 3)
    assert provisional.matcher_disposition == "provisional"
    assert provisional.assignment_state == "candidate"
    assert rejected.role_continuity == 0.0
    assert rejected.matcher_disposition == "rejected"
    assert rejected.assignment_state == "rejected"
    assert rejected.rejection_reasons == ["no_role_continuity"]


def test_same_surface_never_threads_without_same_explicit_key():
    candidate = _candidate(
        _match(_m03_frames("none", same_surfaces=True)),
        "M03",
    )
    assert candidate.role_continuity == 0.0
    assert all(
        item.classification == "disconnected"
        for item in candidate.role_transitions
    )


def _m12_frames(*, with_condition: bool) -> list[FrameInstanceCandidateV1]:
    return [
        _compile(
            _claim(
                "CAUSES",
                20 + index,
                conditions=["under congestion"]
                if with_condition and index == 1
                else [],
            ),
            f"thread:m12:{index}",
            f"thread:m12:{index + 1}",
        )
        for index in range(3)
    ]


def test_m12_condition_must_come_from_own_compiled_claim_field():
    satisfied = _candidate(_match(_m12_frames(with_condition=True)), "M12")
    missing = _candidate(_match(_m12_frames(with_condition=False)), "M12")
    assert satisfied.qualifier_status == "satisfied"
    assert satisfied.matcher_disposition == "confirmed_candidate"
    assert missing.qualifier_status == "required_condition_missing"
    assert missing.matcher_disposition == "rejected"
    assert missing.rejection_reasons == ["required_condition_missing"]


def test_missing_gapped_reordered_and_noncontiguous_inputs_do_not_match():
    frames = _m03_frames("directional")
    assert not [
        item for item in _match(frames[:-1]).candidates if item.motif_id == "M03"
    ]
    gap = _compile(_claim("SIGNALS", 10), "thread:gap:a", "thread:gap:b")
    assert not [
        item
        for item in _match([frames[0], gap, *frames[1:]]).candidates
        if item.motif_id == "M03"
    ]
    assert not [
        item
        for item in _match([frames[1], frames[0], *frames[2:]]).candidates
        if item.motif_id == "M03"
    ]

    bad_indices = [
        FrameSequenceItemV1(
            schema_version="frame_sequence_item.v1",
            sequence_index=index * 2,
            frame_instance_id=frame.frame_instance_id,
        )
        for index, frame in enumerate(frames)
    ]
    with pytest.raises(ValueError, match="sequence indices must be contiguous"):
        match_motifs(
            target_artifact_id="parent:t9-2",
            frame_instances=frames,
            sequence_items=bad_indices,
        )


def test_unknown_frame_motif_and_stage_binding_ids_hard_error():
    frame = _m03_frames("directional")[0]
    payload = frame.model_dump()
    payload["frame_id"] = "MF99"
    with pytest.raises(ValidationError):
        FrameInstanceCandidateV1.model_validate(payload)

    frames = _m03_frames("directional")
    sequence = [
        FrameSequenceItemV1(
            schema_version="frame_sequence_item.v1",
            sequence_index=index,
            frame_instance_id=item.frame_instance_id,
        )
        for index, item in enumerate(frames)
    ]
    bad_motif = copy.deepcopy(load_all()["motif"])
    bad_motif["motifs"][0]["motif_id"] = "M99"
    with pytest.raises(RegistryError, match="publish a new registry version"):
        match_motifs(
            target_artifact_id="parent:t9-2",
            frame_instances=frames,
            sequence_items=sequence,
            motif_registry=bad_motif,
        )

    bad_binding = copy.deepcopy(load_all()["binding"])
    bad_binding["bindings"][0]["stages"][0]["admissible"][0][
        "superframe"
    ] = "MF99"
    with pytest.raises(RegistryError, match="publish a new registry version"):
        match_motifs(
            target_artifact_id="parent:t9-2",
            frame_instances=frames,
            sequence_items=sequence,
            binding_registry=bad_binding,
        )


def test_matcher_replay_receipt_and_coverage_are_honest():
    frames = _m03_frames("partial")
    first = _match(frames)
    second = _match(frames)
    assert first.model_dump_json() == second.model_dump_json()
    receipt = first.receipt()
    assert receipt["sequence_alignment_is_definitional"] is True
    assert receipt["accepted_state_count"] == 0
    assert receipt["dispositions"] == {"provisional": 1}
    assert load_all()["frame_role_binding"]["coverage"][
        "predicate_lane_reachable_superframe_count"
    ] == 8
    coverage = load_all()["motif_matching"]["coverage"]
    assert coverage["generic_matcher_supported_motif_count"] == 12
    assert coverage["predicate_lane_reachable_motif_count"] == 4
    assert coverage["predicate_lane_reachable_motif_ids"] == [
        "M03",
        "M08",
        "M09",
        "M12",
    ]


def test_t9_2_module_has_no_provider_or_durable_write_boundary():
    source = inspect.getsource(frame_motif)
    forbidden_imports = (
        "import httpx",
        "import requests",
        "from pymongo",
        "motor.motor_asyncio",
        "qdrant_client",
        "neo4j import",
    )
    assert not any(item in source for item in forbidden_imports)
    assert "insert_one(" not in source
    assert "update_one(" not in source
    assert "replace_one(" not in source
