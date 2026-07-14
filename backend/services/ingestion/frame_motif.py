"""Side-effect-free T9.2 frame binding and strict motif matching."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, get_args

from models.claim_record import ClaimArgumentV1, ClaimRecordV1
from models.frame_motif import (
    FrameInstanceCandidateV1,
    FrameRoleBindingV1,
    FrameSequenceItemV1,
    MotifCandidateV1,
    MotifMatchResultV1,
    MotifScoreComponentsV1,
    MotifStageMatchV1,
    RoleThreadTransitionV1,
)
from models.hash_taxonomy import namespace_hash
from models.registry_loader import RegistryError, load_all
from models.semantic_resolution import SuperframeRuleMatchV1


FRAME_COMPILER_VERSION = "deterministic_frame_role_compiler.v1"
MOTIF_MATCHER_VERSION = "strict_sequence_exact_thread_matcher.v1"


def _logical_id(prefix: str, artifact_kind: str, natural_keys: dict[str, Any]) -> str:
    digest = namespace_hash(
        "logical-artifact",
        {"artifact_kind": artifact_kind, "natural_keys": natural_keys},
    ).split(":", 1)[1]
    return f"{prefix}:{digest}"


def _assert_claim_argument_role_contract(policy: dict[str, Any]) -> None:
    annotation = ClaimArgumentV1.model_fields["role"].annotation
    code_roles = list(get_args(annotation))
    policy_roles = policy.get("current_claim_argument_role_vocabulary")
    if code_roles != ["subject", "object"] or policy_roles != code_roles:
        raise RegistryError(
            "ClaimArgumentV1 role vocabulary changed; FrameInstance v2 is required"
        )


def _assert_canonical_registry_snapshot(
    name: str,
    observed: dict[str, Any],
    canonical: dict[str, Any],
) -> None:
    if observed != canonical:
        raise RegistryError(
            f"{name} v1 snapshot drifted; publish a new registry version"
        )


def compile_frame_instance(
    claim: ClaimRecordV1,
    rule_match: SuperframeRuleMatchV1,
    *,
    thread_keys_by_filler_ref: Mapping[str, str],
    frame_role_policy: dict[str, Any] | None = None,
) -> FrameInstanceCandidateV1:
    """Bind every legal ClaimArgumentV1 to lossless source/target roles."""

    registries = load_all()
    policy = (
        registries["frame_role_binding"]
        if frame_role_policy is None
        else frame_role_policy
    )
    if policy.get("registry") != "frame_role_binding_policy":
        raise RegistryError("frame compiler received the wrong policy registry")
    if policy.get("version") != "v1":
        raise RegistryError("frame compiler requires frame_role_binding_policy.v1")
    _assert_claim_argument_role_contract(policy)
    _assert_canonical_registry_snapshot(
        "frame_role_binding_policy",
        policy,
        registries["frame_role_binding"],
    )
    if claim.typing_status != "typed" or claim.normalized_predicate is None:
        raise ValueError("frame compiler requires a typed ClaimRecordV1")
    if rule_match.claim_id != claim.claim_id:
        raise ValueError("superframe rule match references a different claim")
    if rule_match.predicate_type != claim.normalized_predicate:
        raise ValueError("superframe rule match predicate disagrees with claim")
    if rule_match.assignment_state != "candidate":  # pragma: no cover - Literal
        raise ValueError("frame compiler consumes candidate rule matches only")

    filler_refs = {item.filler_ref for item in claim.arguments}
    supplied_refs = set(thread_keys_by_filler_ref)
    if supplied_refs - filler_refs:
        raise ValueError(
            "thread-key map references unknown claim filler refs: "
            f"{sorted(supplied_refs - filler_refs)}"
        )
    if filler_refs - supplied_refs:
        raise ValueError(
            "thread-key map is missing claim filler refs: "
            f"{sorted(filler_refs - supplied_refs)}"
        )
    if any(
        not isinstance(value, str) or not value.strip()
        for value in thread_keys_by_filler_ref.values()
    ):
        raise ValueError("thread keys must be explicit nonempty strings")

    frame_registry_hash = namespace_hash("registry", registries["superframe"])
    policy_hash = namespace_hash("registry", policy)
    recipe_hash = namespace_hash(
        "recipe",
        {
            "compiler": FRAME_COMPILER_VERSION,
            "frame_registry_hash": frame_registry_hash,
            "superframe_rule_registry_hash": rule_match.rule_registry_hash,
            "frame_role_binding_policy_hash": policy_hash,
            "thread_key_source": "explicit_caller_mapping",
            "unbound_argument_count": 0,
            "accepted_state": "forbidden",
        },
    )
    frame_instance_id = _logical_id(
        "frame-instance",
        "frame_instance_candidate",
        {
            "claim_id": claim.claim_id,
            "superframe_rule_match_id": rule_match.match_id,
            "frame_id": rule_match.frame_id,
            "frame_role_binding_policy_version": policy["version"],
        },
    )
    bindings: list[FrameRoleBindingV1] = []
    for argument in claim.arguments:
        direction_role = "source" if argument.role == "subject" else "target"
        bindings.append(
            FrameRoleBindingV1(
                schema_version="frame_role_binding.v1",
                binding_id=_logical_id(
                    "frame-role-binding",
                    "frame_role_binding",
                    {
                        "frame_instance_id": frame_instance_id,
                        "claim_argument_role": argument.role,
                        "span_observation_id": argument.span_observation_id,
                        "filler_ref": argument.filler_ref,
                    },
                ),
                claim_id=claim.claim_id,
                claim_argument_role=argument.role,
                relation_direction_role=direction_role,
                filler_kind=argument.filler_kind,
                filler_ref=argument.filler_ref,
                span_observation_id=argument.span_observation_id,
                thread_key=thread_keys_by_filler_ref[argument.filler_ref],
                surface=argument.surface,
                start_char=argument.start_char,
                end_char=argument.end_char,
                evidence_sentence_id=argument.evidence_sentence_id,
            )
        )

    return FrameInstanceCandidateV1(
        schema_version="frame_instance_candidate.v1",
        frame_instance_id=frame_instance_id,
        frame_id=rule_match.frame_id,
        instance_role="primary",
        source_claim_id=claim.claim_id,
        source_rule_match_id=rule_match.match_id,
        source_rule_id=rule_match.rule_id,
        normalized_predicate=claim.normalized_predicate,
        role_bindings=bindings,
        unbound_argument_count=0,
        unbound_argument_refs=[],
        direction="source_to_target",
        polarity=claim.polarity,
        modality=claim.modality,
        conditions=list(claim.conditions),
        exceptions=list(claim.exceptions),
        temporal_cues=list(claim.temporal_cues),
        evidence_sentence_ids=sorted(claim.evidence_sentence_ids),
        source_relation_ids=sorted(claim.source_relation_ids),
        assignment_state="candidate",
        accepted_state_written=False,
        derivation_method="predicate_superframe_rule_role_binding",
        frame_registry_hash=frame_registry_hash,
        superframe_rule_registry_hash=rule_match.rule_registry_hash,
        frame_role_binding_policy_hash=policy_hash,
        compilation_recipe_hash=recipe_hash,
    )


def _transition(
    prior: FrameInstanceCandidateV1,
    following: FrameInstanceCandidateV1,
    transition_index: int,
) -> RoleThreadTransitionV1:
    prior_target = {
        item.thread_key
        for item in prior.role_bindings
        if item.relation_direction_role == "target"
    }
    following_source = {
        item.thread_key
        for item in following.role_bindings
        if item.relation_direction_role == "source"
    }
    directional = prior_target & following_source
    if directional:
        classification = "directional"
        shared = directional
    else:
        prior_all = {item.thread_key for item in prior.role_bindings}
        following_all = {item.thread_key for item in following.role_bindings}
        shared = prior_all & following_all
        classification = "shared_participant" if shared else "disconnected"
    return RoleThreadTransitionV1(
        schema_version="role_thread_transition.v1",
        transition_index=transition_index,
        prior_frame_instance_id=prior.frame_instance_id,
        next_frame_instance_id=following.frame_instance_id,
        classification=classification,
        shared_thread_keys=sorted(shared),
    )


def _validate_matcher_registries(
    motif_registry: dict[str, Any],
    binding_registry: dict[str, Any],
    matching_policy: dict[str, Any],
) -> None:
    if motif_registry.get("registry") != "motif_registry":
        raise RegistryError("motif matcher received the wrong motif registry")
    if motif_registry.get("version") != "v1":
        raise RegistryError("motif matcher requires motif_registry.v1")
    if binding_registry.get("registry") != "motif_stage_superframe_binding":
        raise RegistryError("motif matcher received the wrong stage-binding registry")
    if binding_registry.get("version") != "v1":
        raise RegistryError("motif matcher requires stage bindings v1")
    if matching_policy.get("registry") != "motif_matching_policy":
        raise RegistryError("motif matcher received the wrong policy registry")
    if matching_policy.get("version") != "v1":
        raise RegistryError("motif matcher requires motif_matching_policy.v1")


def match_motifs(
    *,
    target_artifact_id: str,
    frame_instances: Iterable[FrameInstanceCandidateV1],
    sequence_items: Iterable[FrameSequenceItemV1],
    motif_registry: dict[str, Any] | None = None,
    binding_registry: dict[str, Any] | None = None,
    matching_policy: dict[str, Any] | None = None,
) -> MotifMatchResultV1:
    """Match strict contiguous windows and retain exact role-thread evidence."""

    if not target_artifact_id.strip():
        raise ValueError("target_artifact_id must be nonempty")
    registries = load_all()
    motifs_data = registries["motif"] if motif_registry is None else motif_registry
    bindings_data = (
        registries["binding"] if binding_registry is None else binding_registry
    )
    policy = (
        registries["motif_matching"]
        if matching_policy is None
        else matching_policy
    )
    _validate_matcher_registries(motifs_data, bindings_data, policy)
    _assert_canonical_registry_snapshot(
        "motif_registry",
        motifs_data,
        registries["motif"],
    )
    _assert_canonical_registry_snapshot(
        "motif_stage_superframe_binding",
        bindings_data,
        registries["binding"],
    )
    _assert_canonical_registry_snapshot(
        "motif_matching_policy",
        policy,
        registries["motif_matching"],
    )

    frames = list(frame_instances)
    frame_by_id = {item.frame_instance_id: item for item in frames}
    if len(frame_by_id) != len(frames):
        raise ValueError("frame instance IDs must be unique")
    ordered_items = list(sequence_items)
    indices = [item.sequence_index for item in ordered_items]
    if indices != list(range(len(indices))):
        raise ValueError("caller-supplied sequence indices must be contiguous")
    sequence_frame_ids = [item.frame_instance_id for item in ordered_items]
    if len(sequence_frame_ids) != len(set(sequence_frame_ids)):
        raise ValueError("frame sequence instance IDs must be unique")
    if set(sequence_frame_ids) != set(frame_by_id):
        raise ValueError(
            "frame instances and explicit sequence must have exact closure"
        )
    ordered_frames = [frame_by_id[item] for item in sequence_frame_ids]

    motifs = {item["motif_id"]: item for item in motifs_data.get("motifs") or []}
    if len(motifs) != len(motifs_data.get("motifs") or []):
        raise RegistryError("motif IDs must be unique")
    binding_rows = bindings_data.get("bindings") or []
    bindings = {item["motif_id"]: item for item in binding_rows}
    if len(bindings) != len(binding_rows) or set(bindings) != set(motifs):
        raise RegistryError("motif stage-binding coverage is not exact")

    motif_hash = namespace_hash("registry", motifs_data)
    binding_hash = namespace_hash("registry", bindings_data)
    policy_hash = namespace_hash("registry", policy)
    recipe_hash = namespace_hash(
        "recipe",
        {
            "matcher": MOTIF_MATCHER_VERSION,
            "motif_registry_hash": motif_hash,
            "stage_binding_registry_hash": binding_hash,
            "motif_matching_policy_hash": policy_hash,
            "sequence_tolerance": "strict_contiguous_full_coverage",
            "role_threading": "exact_thread_keys",
            "metrics": ["sequence_alignment", "role_continuity"],
            "fused_final_score": None,
            "accepted_state": "forbidden",
        },
    )

    candidates: list[MotifCandidateV1] = []
    windows_scanned = 0
    known_superframes = {
        item["mf_id"] for item in registries["superframe"]["superframes"]
    }
    for motif_id in sorted(motifs):
        motif = motifs[motif_id]
        stage_specs = bindings[motif_id].get("stages") or []
        if [item.get("stage") for item in stage_specs] != motif.get("stages"):
            raise RegistryError(f"binding stages for {motif_id} drifted")
        for stage in stage_specs:
            admissible = stage.get("admissible") or []
            stage_frames = [item.get("superframe") for item in admissible]
            if not admissible or len(stage_frames) != len(set(stage_frames)):
                raise RegistryError(
                    f"{motif_id}/{stage.get('stage')}: invalid frame bindings"
                )
            if any(frame_id not in known_superframes for frame_id in stage_frames):
                raise RegistryError(
                    f"{motif_id}/{stage.get('stage')}: unknown superframe binding"
                )
            if sum(item.get("tier") == "dominant" for item in admissible) != 1:
                raise RegistryError(
                    f"{motif_id}/{stage.get('stage')}: dominant tier drifted"
                )
            if any(
                item.get("tier") not in {"dominant", "admissible"}
                for item in admissible
            ):
                raise RegistryError(
                    f"{motif_id}/{stage.get('stage')}: unknown binding tier"
                )
        stage_count = len(stage_specs)
        if stage_count < 2:
            raise RegistryError(f"motif {motif_id} requires at least two stages")
        possible_windows = max(0, len(ordered_frames) - stage_count + 1)
        windows_scanned += possible_windows
        for start in range(possible_windows):
            window = ordered_frames[start : start + stage_count]
            stage_matches: list[MotifStageMatchV1] = []
            for stage_index, (stage, frame) in enumerate(zip(stage_specs, window)):
                admissible = stage.get("admissible") or []
                matching_rows = [
                    item
                    for item in admissible
                    if item.get("superframe") == frame.frame_id
                ]
                if len(matching_rows) > 1:
                    raise RegistryError(
                        f"{motif_id}/{stage['stage']}: duplicate frame binding"
                    )
                if not matching_rows:
                    break
                tier = matching_rows[0].get("tier")
                if tier not in {"dominant", "admissible"}:
                    raise RegistryError(
                        f"{motif_id}/{stage['stage']}: unknown match tier"
                    )
                stage_matches.append(
                    MotifStageMatchV1(
                        schema_version="motif_stage_match.v1",
                        stage_index=stage_index,
                        stage=stage["stage"],
                        frame_instance_id=frame.frame_instance_id,
                        frame_id=frame.frame_id,
                        match_tier=tier,
                    )
                )
            if len(stage_matches) != stage_count:
                continue

            transitions = [
                _transition(prior, following, index)
                for index, (prior, following) in enumerate(zip(window, window[1:]))
            ]
            directional_count = sum(
                item.classification == "directional" for item in transitions
            )
            shared_count = sum(
                item.classification == "shared_participant" for item in transitions
            )
            disconnected_count = sum(
                item.classification == "disconnected" for item in transitions
            )
            connected_count = directional_count + shared_count
            transition_count = len(transitions)
            qualifier_status = "not_applicable"
            rejection_reasons: list[str] = []
            if connected_count == 0:
                rejection_reasons.append("no_role_continuity")
            if motif_id == "M12":
                if any(frame.conditions for frame in window):
                    qualifier_status = "satisfied"
                else:
                    qualifier_status = "required_condition_missing"
                    rejection_reasons.append("required_condition_missing")
            if rejection_reasons:
                disposition = "rejected"
                assignment_state = "rejected"
            elif connected_count == transition_count:
                disposition = "confirmed_candidate"
                assignment_state = "candidate"
            else:
                disposition = "provisional"
                assignment_state = "candidate"

            frame_ids = [frame.frame_instance_id for frame in window]
            candidates.append(
                MotifCandidateV1(
                    schema_version="motif_candidate.v1",
                    motif_candidate_id=_logical_id(
                        "motif-candidate",
                        "motif_candidate",
                        {
                            "target_artifact_id": target_artifact_id,
                            "motif_id": motif_id,
                            "frame_instance_ids": frame_ids,
                            "motif_matching_policy_version": policy["version"],
                        },
                    ),
                    motif_id=motif_id,
                    frame_instance_ids=frame_ids,
                    source_claim_ids=sorted(
                        {frame.source_claim_id for frame in window}
                    ),
                    stage_matches=stage_matches,
                    role_transitions=transitions,
                    score_components=MotifScoreComponentsV1(
                        canonical_stage_count=stage_count,
                        matched_stage_count=len(stage_matches),
                        dominant_stage_count=sum(
                            item.match_tier == "dominant" for item in stage_matches
                        ),
                        admissible_stage_count=sum(
                            item.match_tier == "admissible" for item in stage_matches
                        ),
                        transition_count=transition_count,
                        directional_transition_count=directional_count,
                        shared_participant_transition_count=shared_count,
                        disconnected_transition_count=disconnected_count,
                    ),
                    sequence_alignment=len(stage_matches) / stage_count,
                    sequence_alignment_interpretation=(
                        "definitional_under_strict_v1_not_quality_signal"
                    ),
                    role_continuity=connected_count / transition_count,
                    matcher_disposition=disposition,
                    assignment_state=assignment_state,
                    rejection_reasons=sorted(rejection_reasons),
                    qualifier_status=qualifier_status,
                    evidence_sentence_ids=sorted(
                        {
                            evidence_id
                            for frame in window
                            for evidence_id in frame.evidence_sentence_ids
                        }
                    ),
                    accepted_state_written=False,
                    derivation_method="strict_sequence_and_exact_role_threading",
                    motif_registry_hash=motif_hash,
                    stage_binding_registry_hash=binding_hash,
                    motif_matching_policy_hash=policy_hash,
                    matcher_recipe_hash=recipe_hash,
                )
            )

    return MotifMatchResultV1(
        schema_version="motif_match_result.v1",
        target_artifact_id=target_artifact_id,
        sequence_items=ordered_items,
        candidates=sorted(candidates, key=lambda item: item.motif_candidate_id),
        windows_scanned=windows_scanned,
        sequence_aligned_window_count=len(candidates),
        matcher_recipe_hash=recipe_hash,
    )
