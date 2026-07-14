"""T9.1 deterministic domain + predicate→superframe candidate boundary."""

from __future__ import annotations

import copy
import inspect

import pytest
from pydantic import ValidationError

from models.claim_record import ClaimArgumentV1, ClaimRecordV1
from models.registry_loader import RegistryError, load_all
from models.semantic_resolution import DomainSignalV1
from services.ingestion import semantic_resolution
from services.ingestion.corpus_lexicon import normalize_identity
from services.ingestion.semantic_resolution import (
    build_domain_affinity_serve_view,
    resolve_domains,
    resolve_superframe_rule,
)


DIRECT_FRAME_BY_PREDICATE = {
    "CAUSES": "MF04",
    "INFLUENCES": "MF04",
    "INCREASES": "MF04",
    "DECREASES": "MF04",
    "UPDATES": "MF07",
    "SIGNALS": "MF02",
    "MEASURES": "MF03",
    "COMPARES_AGAINST": "MF03",
    "ENABLES": "MF04",
    "INHIBITS": "MF04",
    "REQUIRES": "MF09",
    "CONSTRAINS": "MF09",
    "RESULTS_IN": "MF04",
    "APPLIES_UNDER": "MF09",
    "PART_OF": "MF16",
    "USED_FOR": "MF06",
}


def _signal(
    signal_id: str,
    label: str,
    signal_kind: str,
    *,
    evidence_ref_id: str | None = None,
    claim_id: str = "claim:1",
) -> DomainSignalV1:
    return DomainSignalV1(
        schema_version="domain_signal.v1",
        signal_id=signal_id,
        label=label,
        signal_kind=signal_kind,
        evidence_ref_ids=[evidence_ref_id or f"evidence:{signal_id}"],
        supporting_claim_ids=[claim_id] if signal_kind == "claim_concept" else [],
    )


def _claim(
    predicate: str | None,
    *,
    claim_id: str = "claim:test",
    subject: str = "A process",
) -> ClaimRecordV1:
    typed = predicate is not None
    return ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id=claim_id,
        document_id="doc:test",
        child_id="child:test",
        proposition_text=f"{subject} changes the baseline",
        canonical_proposition=f"{subject.lower()} change baseline",
        claim_type="causal" if typed else "description_or_observation",
        predicate_observation_id="predicate-observation:test",
        predicate_id="predicate:test" if typed else None,
        predicate_surface="changes",
        predicate_lemma="change",
        normalized_predicate=predicate,
        typing_status="typed" if typed else "untyped",
        arguments=[
            ClaimArgumentV1(
                role="subject",
                filler_kind="entity_mention",
                filler_ref="mention:subject",
                span_observation_id="span:subject",
                surface=subject,
                start_char=0,
                end_char=len(subject),
                evidence_sentence_id="evidence:1",
            ),
            ClaimArgumentV1(
                role="object",
                filler_kind="entity_mention",
                filler_ref="mention:object",
                span_observation_id="span:object",
                surface="the baseline",
                start_char=len(subject) + 9,
                end_char=len(subject) + 21,
                evidence_sentence_id="evidence:1",
            ),
        ],
        polarity="positive",
        modality="asserted",
        assertion_mode="reported",
        conditions=[],
        exceptions=[],
        temporal_cues=[],
        evidence_sentence_ids=["evidence:1"],
        source_relation_ids=[],
        scope_hash="sha256:scope-fixture",
        knowledge_status="candidate",
        validation_status="candidate",
    )


def _types(object_type: str = "QUALITY") -> dict[str, str]:
    return {
        "mention:subject": "BEHAVIOR",
        "mention:object": object_type,
    }


def test_exact_domain_resolution_merges_evidence_and_claim_local_dominates():
    result = resolve_domains(
        target_artifact_id="parent:test",
        signals=[
            _signal("s1", "Data Science", "claim_concept"),
            _signal("s2", "Technology and Engineered Systems", "section_heading"),
            _signal("s3", "Markets", "section_heading"),
            _signal("s4", "Data Sciences", "section_heading"),
            _signal("s5", "Data Sciences", "section_heading"),
        ],
        context_profile_ids=["profile:2", "profile:1", "profile:1"],
    )

    assert [item.domain_id for item in result.assignments] == ["D06", "D09"]
    economic, technology = result.assignments
    assert economic.assignment_role == "supporting"
    assert economic.derivation_method == "exact_section_heading"
    assert economic.score_components.exact_heading_matches == 1
    assert technology.assignment_role == "dominant"
    assert technology.derivation_method == "exact_claim_concept_and_heading"
    assert technology.matched_signal_ids == ["s1", "s2"]
    assert technology.score_components.model_dump() == {
        "exact_claim_concept_matches": 1,
        "exact_heading_matches": 1,
        "claim_evidence_ref_count": 1,
        "context_evidence_ref_count": 1,
    }
    assert "score" not in technology.model_dump()
    assert result.context_profile_ids == ["profile:1", "profile:2"]
    assert result.receipt() == {
        "resolution_recipe_hash": (
            "sha256:1bd659e406f7ced96d5fb4de0a76acddab88d39d92fcf0783a311cd93fd98e62"
        ),
        "assignment_count": 2,
        "dominant_count": 1,
        "supporting_count": 1,
        "unresolved_count": 2,
        "top_unresolved_terms": [
            {"normalized_term": "data sciences", "count": 2}
        ],
        "unresolved_destination": "CP5_alias_registry_evidence",
        "unresolved_acted_on": False,
    }


@pytest.mark.parametrize(
    "label",
    [
        "Data Sciences",  # stem/plural near-match
        "Artificial",  # substring of Artificial intelligence
        "behavioral",  # partial token
        "Market",  # singular/fuzzy near-match to Markets
    ],
)
def test_fuzzy_substring_and_stem_domain_near_matches_abstain(label):
    result = resolve_domains(
        target_artifact_id="parent:test",
        signals=[_signal("near", label, "section_heading")],
    )
    assert result.assignments == []
    assert len(result.unresolved_signals) == 1
    assert result.unresolved_signals[0].normalized_term == normalize_identity(label)
    assert result.unresolved_signals[0].assignment_state == "unresolved"


def test_domain_normalizer_is_the_cp5_function_and_divergence_is_surfaced():
    policy = load_all()["domain_resolution"]
    assert semantic_resolution.normalize_identity is normalize_identity
    assert policy["normalizer"]["normalizer_id"] == (
        "corpus_lexicon.normalize_identity.v1"
    )
    assert "canonicalize_entity_name" in policy["normalizer"][
        "graph_entity_id_divergence"
    ]
    assert policy["normalizer"]["reconciliation_owner"] == (
        "CP5 versioned alias registry"
    )


def test_predicate_is_not_a_domain_signal_field():
    payload = _signal("s1", "Data Science", "claim_concept").model_dump()
    payload["predicate_type"] = "CAUSES"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DomainSignalV1.model_validate(payload)


@pytest.mark.parametrize("predicate,frame_id", DIRECT_FRAME_BY_PREDICATE.items())
def test_every_direct_controlled_predicate_routes_to_confirmed_frame(
    predicate,
    frame_id,
):
    result = resolve_superframe_rule(
        _claim(predicate, claim_id=f"claim:{predicate}"),
        entity_types_by_mention_id=_types(),
    )
    assert result.explicit_abstention_reason is None
    assert len(result.matches) == 1
    assert result.matches[0].frame_id == frame_id
    assert result.matches[0].assignment_state == "candidate"
    assert result.matches[0].terminal is True


def test_associated_with_explicitly_abstains_and_never_becomes_a_frame():
    result = resolve_superframe_rule(
        _claim("ASSOCIATED_WITH"),
        entity_types_by_mention_id=_types(),
    )
    assert result.matches == []
    assert result.explicit_abstention_reason == (
        "generic_association_is_not_a_mechanism"
    )


def test_untyped_claim_explicitly_abstains():
    result = resolve_superframe_rule(
        _claim(None),
        entity_types_by_mention_id={},
    )
    assert result.predicate_type is None
    assert result.matches == []
    assert result.explicit_abstention_reason == "claim_is_untyped"


def test_mf15_specialization_is_higher_priority_terminal_and_data_driven():
    specialized = resolve_superframe_rule(
        _claim("DECREASES", subject="Repeated discounting"),
        entity_types_by_mention_id=_types("BASELINE"),
    )
    assert len(specialized.matches) == 1
    assert specialized.matches[0].frame_id == "MF15"
    assert specialized.matches[0].priority == 200
    assert specialized.matches[0].rule_id == (
        "predicate_frame.decreases_cumulative_baseline.v1"
    )

    no_baseline = resolve_superframe_rule(
        _claim("DECREASES", subject="Repeated discounting"),
        entity_types_by_mention_id=_types("QUALITY"),
    )
    no_marker = resolve_superframe_rule(
        _claim("DECREASES", subject="Discounting"),
        entity_types_by_mention_id=_types("BASELINE"),
    )
    near_marker = resolve_superframe_rule(
        _claim("DECREASES", subject="Accumulative discounting"),
        entity_types_by_mention_id=_types("BASELINE"),
    )
    assert [no_baseline.matches[0].frame_id, no_marker.matches[0].frame_id] == [
        "MF04",
        "MF04",
    ]
    assert near_marker.matches[0].frame_id == "MF04"


def test_used_for_is_candidate_but_carries_owner_attention_flag():
    result = resolve_superframe_rule(
        _claim("USED_FOR"),
        entity_types_by_mention_id=_types(),
    )
    assert result.matches[0].frame_id == "MF06"
    assert result.matches[0].owner_attention is True
    assert result.receipt()["owner_attention_count"] == 1


def test_affinity_change_only_changes_quarantined_serve_view():
    resolution = resolve_domains(
        target_artifact_id="parent:test",
        signals=[_signal("s1", "Data Science", "claim_concept")],
    )
    claim = _claim("SIGNALS")
    frame_before = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id=_types(),
    )
    resolution_before = resolution.model_dump(mode="json")
    view_before = build_domain_affinity_serve_view(resolution)

    changed_affinity = copy.deepcopy(load_all()["affinity"])
    d09 = next(
        row for row in changed_affinity["affinities"] if row["domain_id"] == "D09"
    )
    d09["dominant_superframes"].append("MF01")
    view_after = build_domain_affinity_serve_view(
        resolution,
        affinity_registry=changed_affinity,
    )
    frame_after = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id=_types(),
    )

    assert resolution.model_dump(mode="json") == resolution_before
    assert view_before.affinity_registry_hash != view_after.affinity_registry_hash
    assert view_before.priors != view_after.priors
    assert frame_before == frame_after
    assert frame_before.matches[0].match_id == frame_after.matches[0].match_id
    assert frame_before.resolution_recipe_hash == frame_after.resolution_recipe_hash
    assert view_after.serve_only is True
    assert view_after.excluded_from_semantic_identity is True
    assert view_after.excluded_from_acceptance is True


def test_unknown_registry_and_rule_input_ids_hard_error():
    bad_domain = copy.deepcopy(load_all()["domain"])
    bad_domain["domains"][0]["domain_id"] = "D99"
    with pytest.raises(RegistryError, match="unknown domain"):
        resolve_domains(
            target_artifact_id="parent:test",
            signals=[_signal("s1", "Data Science", "claim_concept")],
            domain_registry=bad_domain,
        )

    resolution = resolve_domains(
        target_artifact_id="parent:test",
        signals=[_signal("s1", "Data Science", "claim_concept")],
    )
    bad_affinity = copy.deepcopy(load_all()["affinity"])
    bad_affinity["affinities"][8]["dominant_superframes"][0] = "MF99"
    with pytest.raises(RegistryError, match="unknown superframe"):
        build_domain_affinity_serve_view(
            resolution,
            affinity_registry=bad_affinity,
        )

    bad_rules = copy.deepcopy(load_all()["superframe_rule"])
    bad_rules["rules"][0]["frame_id"] = "MF99"
    with pytest.raises(RegistryError, match="unknown superframe ID"):
        resolve_superframe_rule(
            _claim("SIGNALS"),
            entity_types_by_mention_id=_types(),
            rule_registry=bad_rules,
        )

    with pytest.raises(RegistryError, match="unknown claim mention IDs"):
        resolve_superframe_rule(
            _claim("SIGNALS"),
            entity_types_by_mention_id={**_types(), "mention:ghost": "CONCEPT"},
        )
    with pytest.raises(RegistryError, match="unknown entity types"):
        resolve_superframe_rule(
            _claim("SIGNALS"),
            entity_types_by_mention_id={
                "mention:subject": "BEHAVIOR",
                "mention:object": "THINGY",
            },
        )


def test_replay_is_byte_identical_and_recipe_hashes_are_frozen():
    signals = [
        _signal("s1", "Data Science", "claim_concept"),
        _signal("s2", "Market", "section_heading"),
    ]
    first_domain = resolve_domains(
        target_artifact_id="parent:test",
        signals=signals,
    )
    second_domain = resolve_domains(
        target_artifact_id="parent:test",
        signals=signals,
    )
    assert first_domain.model_dump_json() == second_domain.model_dump_json()
    assert first_domain.resolution_recipe_hash == (
        "sha256:1bd659e406f7ced96d5fb4de0a76acddab88d39d92fcf0783a311cd93fd98e62"
    )

    claim = _claim("SIGNALS")
    first_frame = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id=_types(),
    )
    second_frame = resolve_superframe_rule(
        claim,
        entity_types_by_mention_id=_types(),
    )
    assert first_frame.model_dump_json() == second_frame.model_dump_json()
    assert first_frame.resolution_recipe_hash == (
        "sha256:9e80a6d4f6b9f09f39dcdd39b5432be7cd90d49e1a53d37fb7ef5a89a160c26a"
    )


def test_t9_1_module_has_no_provider_or_durable_write_boundary():
    source = inspect.getsource(semantic_resolution)
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
