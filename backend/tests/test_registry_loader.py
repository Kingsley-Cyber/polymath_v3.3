"""Owner-registry loader: cross-validation, resolver, and tamper-evidence.

The FROZEN_HASHES below pin the v1 owner-delivered registry snapshots. A hash
mismatch means a registry file was edited in place — which is forbidden: any
change ships as a NEW version file plus updated goldens, never a silent edit.
"""

from __future__ import annotations

import copy
from typing import get_args

import pytest

import models.registry_loader as registry_loader
from models.claim_record import ClaimArgumentV1
from models.registry_loader import (
    RegistryError,
    admissible_superframes,
    domain,
    domain_affinity_priors,
    domain_resolution_policy,
    embedding_instruction_profile,
    frame_role_binding_policy,
    is_controlled_predicate,
    is_entity_type,
    latent_budget,
    load_all,
    motif,
    motif_matching_policy,
    normalize_predicate_lemma,
    registry_hashes,
    superframe,
    superframe_rule_registry,
)

FROZEN_HASHES = {
    "domain": "sha256:8e3c8c6e7d3b02bc689e7275a7a63877c0702b3b8d1601b59bfc2d1d0069dadb",
    "superframe": "sha256:dc386c970b57515a3e0e3371540ae21b1dfa33e71c961383efc1a124363c4d68",
    "affinity": "sha256:7894b6ecc0179e192d90a3c41b416de72da0a65dae5920f46e73cea25e2951da",
    "motif": "sha256:a1d79691bfa23628a3548a03f048c62f3f83df8e27b6fa3d80b774aa72cb7960",
    "vocab": "sha256:bd39855c608e2ce677587c63603eb51caef407fa422cb6575b549dcf68216aa5",
    "latent_policy": "sha256:1bd709eed623b1b6191a4563bd736e1a20544608b1ef94d9c5603f21d3debcd9",
    "binding": "sha256:2811ff011e38762b349b9d14a1f0352edc798b683adf8673106535bcd370c797",
    "embedding_instruction": "sha256:0269d30bf0f852f489e15a8f2dc6b19a8b83b62adb49e346828ce54fc5e89f51",
    "predicate_normalization": "sha256:a0870e5d4cd5f315719245c301ad074824857115ce6f1b9dd7a7d45cd6ca030d",
    "domain_resolution": "sha256:1c54da7c132562c25ab71ddce2cf27253f8405fc0c6a2e7c47f442557d8ced89",
    "superframe_rule": "sha256:7ad83a5735bec13baafef89851bac50f22420b89bbe617e86921a7bdf2dc89c8",
    "frame_role_binding": "sha256:104bbb48072ea4e9bdcd15313de2a74bb9a0ca04bdb86c20491024746e6dd540",
    "motif_matching": "sha256:03b4dbf937008a8f8d50bca6786b46aacfc890a07efaca797d80b68c34eec2a7",
}


def test_load_all_valid():
    data = load_all()
    assert len(data["domain"]["domains"]) == 16
    assert len(data["superframe"]["superframes"]) == 16
    assert len(data["motif"]["motifs"]) == 12


def test_snapshot_hashes_frozen():
    assert registry_hashes() == FROZEN_HASHES


def test_every_motif_stage_has_exactly_one_dominant():
    data = load_all()
    for binding in data["binding"]["bindings"]:
        for stage in binding["stages"]:
            dominants = [a for a in stage["admissible"] if a["tier"] == "dominant"]
            assert len(dominants) == 1, (binding["motif_id"], stage["stage"])


def test_resolver_lookups():
    assert domain("D06")["name"] == "Economic and Commercial Systems"
    assert superframe("MF15")["name"] == "Accumulation, Threshold, and Path Dependence"
    assert motif("M08")["stages"] == ["STOCK/FLOW", "THRESHOLD", "STATE TRANSITION"]
    adm = admissible_superframes("M02", "ATTENTION")
    assert adm[0] == {"superframe": "MF02", "tier": "dominant"}
    assert {"superframe": "MF01", "tier": "admissible"} in adm


def test_set_valued_binding_preserved_for_contested_stages():
    contested = [
        ("M02", "ATTENTION"),
        ("M03", "OUTCOME"),
        ("M05", "SCARCITY"),
        ("M06", "SOCIAL FEEDBACK"),
        ("M07", "RETENTION"),
        ("M10", "PROXY/HEURISTIC"),
        ("M11", "INTERDEPENDENCE"),
    ]
    for motif_id, stage in contested:
        assert len(admissible_superframes(motif_id, stage)) == 2, (motif_id, stage)


def test_affinity_priors_serve_only():
    priors = domain_affinity_priors("D16")
    assert priors == ["MF04", "MF08", "MF13", "MF15", "MF16"]


def test_domain_resolution_policy_is_exact_only_and_non_scoring():
    policy = domain_resolution_policy()
    assert policy["authority"] == "executor-proposed, owner-ratifiable"
    assert policy["owner_ratification_required"] is True
    assert policy["changes_require_new_version"] is True
    assert policy["normalizer"]["implementation"] == (
        "services.ingestion.corpus_lexicon.normalize_identity"
    )
    assert policy["predicate_policy"]["domain_bearing"] is False
    assert policy["scalar_score"] is None
    assert policy["cardinality_cap"] is None
    assert policy["affinity_quarantine"] == {
        "source_registry": "domain_superframe_affinity.v1",
        "serve_only": True,
        "may_assign_domain": False,
        "may_assign_or_forbid_superframe": False,
        "excluded_from_artifact_identity": True,
        "excluded_from_acceptance": True,
    }
    owner_terms = sum(
        1 + len(item["members"]) for item in load_all()["domain"]["domains"]
    )
    assert owner_terms == 162


def test_superframe_rule_registry_has_honest_controlled_coverage():
    registry = superframe_rule_registry()
    coverage = registry["coverage"]
    assert coverage["controlled_predicate_count"] == 17
    assert coverage["rule_covered_predicate_count"] == 16
    assert coverage["explicit_abstention_count"] == 1
    assert coverage["reachable_superframe_count"] == 8
    assert coverage["total_superframe_count"] == 16
    assert coverage["reachable_superframe_ids"] == [
        "MF02",
        "MF03",
        "MF04",
        "MF06",
        "MF07",
        "MF09",
        "MF15",
        "MF16",
    ]
    assert registry["abstentions"] == [
        {
            "predicate": "ASSOCIATED_WITH",
            "reason": "generic_association_is_not_a_mechanism",
            "source_line": (
                "Senior-confirmed junk-floor policy: generic association "
                "cannot become a frame"
            ),
        }
    ]
    used_for = [
        rule for rule in registry["rules"] if "USED_FOR" in rule["predicates"]
    ]
    assert len(used_for) == 1
    assert used_for[0]["frame_id"] == "MF06"
    assert used_for[0]["owner_attention"] is True


def test_frame_role_binding_policy_pins_current_claim_contract():
    policy = frame_role_binding_policy()
    code_roles = list(get_args(ClaimArgumentV1.model_fields["role"].annotation))
    assert code_roles == ["subject", "object"]
    assert policy["current_claim_argument_role_vocabulary"] == code_roles
    assert policy["hard_role_vocabulary_check"] is True
    assert policy["role_bindings"] == [
        {
            "claim_argument_role": "subject",
            "relation_direction_role": "source",
        },
        {
            "claim_argument_role": "object",
            "relation_direction_role": "target",
        },
    ]
    assert policy["unbound_argument_policy"] == {
        "current_unbound_argument_count": 0,
        "interpretation": (
            "definitional_under_claim_record_v1_subject_object_literal"
        ),
        "future_role_behavior": "hard_error_requires_frame_instance_v2",
    }
    assert policy["coverage"]["predicate_lane_reachable_superframe_count"] == 8
    assert policy["coverage"]["total_superframe_count"] == 16


def test_motif_matching_policy_is_strict_separate_and_coverage_honest():
    policy = motif_matching_policy()
    tolerance = policy["sequence_tolerance"]
    assert tolerance["maximum_missing_stages"] == 0
    assert tolerance["maximum_intervening_frames_per_transition"] == 0
    assert tolerance["candidate_generation_tiers"] == [
        "dominant",
        "admissible",
    ]
    assert policy["metrics"]["metrics_must_remain_separate"] is True
    assert policy["metrics"]["fused_final_score"] is None
    assert policy["metrics"]["sequence_alignment"]["interpretation"] == (
        "definitional_under_strict_v1_not_quality_signal"
    )
    assert policy["candidate_lane_dispositions"][
        "confirmed_candidate_is_accepted"
    ] is False
    assert policy["coverage"]["generic_matcher_supported_motif_count"] == 12
    assert policy["coverage"]["predicate_lane_reachable_motif_ids"] == [
        "M03",
        "M08",
        "M09",
        "M12",
    ]
    assert policy["coverage"]["predicate_lane_reachable_motif_count"] == 4


def _mutated_registries() -> dict[str, dict]:
    return {
        name: copy.deepcopy(registry_loader._read(name))
        for name in registry_loader.FILES
    }


def test_superframe_rule_unknown_ids_hard_error(monkeypatch):
    data = _mutated_registries()
    data["superframe_rule"]["rules"][0]["frame_id"] = "MF99"
    monkeypatch.setattr(registry_loader, "_read", lambda name: data[name])
    registry_loader.load_all.cache_clear()
    with pytest.raises(RegistryError, match="unknown superframe"):
        registry_loader.load_all()
    registry_loader.load_all.cache_clear()


def test_superframe_rule_dishonest_coverage_hard_error(monkeypatch):
    data = _mutated_registries()
    data["superframe_rule"]["coverage"]["reachable_superframe_count"] = 16
    monkeypatch.setattr(registry_loader, "_read", lambda name: data[name])
    registry_loader.load_all.cache_clear()
    with pytest.raises(RegistryError, match="reachable count is dishonest"):
        registry_loader.load_all()
    registry_loader.load_all.cache_clear()


def test_domain_affinity_quarantine_drift_hard_error(monkeypatch):
    data = _mutated_registries()
    data["domain_resolution"]["affinity_quarantine"]["may_assign_domain"] = True
    monkeypatch.setattr(registry_loader, "_read", lambda name: data[name])
    registry_loader.load_all.cache_clear()
    with pytest.raises(RegistryError, match="affinity quarantine drifted"):
        registry_loader.load_all()
    registry_loader.load_all.cache_clear()


def test_frame_role_vocabulary_drift_hard_error(monkeypatch):
    data = _mutated_registries()
    data["frame_role_binding"]["current_claim_argument_role_vocabulary"].append(
        "instrument"
    )
    monkeypatch.setattr(registry_loader, "_read", lambda name: data[name])
    registry_loader.load_all.cache_clear()
    with pytest.raises(RegistryError, match="role vocabulary policy drifted"):
        registry_loader.load_all()
    registry_loader.load_all.cache_clear()


def test_motif_fused_score_drift_hard_error(monkeypatch):
    data = _mutated_registries()
    data["motif_matching"]["metrics"]["fused_final_score"] = 0.75
    monkeypatch.setattr(registry_loader, "_read", lambda name: data[name])
    registry_loader.load_all.cache_clear()
    with pytest.raises(RegistryError, match="dual-metric policy drifted"):
        registry_loader.load_all()
    registry_loader.load_all.cache_clear()


def test_motif_coverage_drift_hard_error(monkeypatch):
    data = _mutated_registries()
    data["motif_matching"]["coverage"][
        "predicate_lane_reachable_motif_count"
    ] = 12
    monkeypatch.setattr(registry_loader, "_read", lambda name: data[name])
    with pytest.raises(RegistryError, match="reachable motif count is dishonest"):
        registry_loader.load_all()
    registry_loader.load_all.cache_clear()


def test_vocab_membership():
    assert is_controlled_predicate("CAUSES")
    assert not is_controlled_predicate("VIBES_WITH")
    assert is_entity_type("BASELINE")
    assert not is_entity_type("THINGY")


def test_predicate_normalization_is_conservative_and_versioned():
    assert normalize_predicate_lemma(" Lower ") == {
        "lemma": "lower",
        "predicate_type": "DECREASES",
        "registry": "predicate_normalization",
        "registry_version": "v1",
        "authority": "executor-proposed, owner-ratifiable",
    }
    assert normalize_predicate_lemma("increase")["predicate_type"] == "INCREASES"
    assert normalize_predicate_lemma("vibes") is None
    assert normalize_predicate_lemma("") is None


def test_predicate_normalization_seed_counts_and_unmapped_classes():
    rows = load_all()["predicate_normalization"]["normalizations"]
    counts = {row["predicate_type"]: len(row["lemmas"]) for row in rows}
    assert counts == {
        "CAUSES": 1,
        "INFLUENCES": 1,
        "INCREASES": 3,
        "DECREASES": 3,
        "UPDATES": 2,
        "SIGNALS": 1,
        "MEASURES": 2,
        "COMPARES_AGAINST": 2,
        "ENABLES": 1,
        "INHIBITS": 1,
        "REQUIRES": 2,
        "CONSTRAINS": 2,
        "RESULTS_IN": 0,
        "APPLIES_UNDER": 0,
        "PART_OF": 0,
        "USED_FOR": 0,
        "ASSOCIATED_WITH": 2,
    }


def test_latent_budgets():
    assert latent_budget("parent_group") == {
        "raw_generated": [5, 12],
        "retained_distinct": [3, 8],
    }
    with pytest.raises(RegistryError):
        latent_budget("galaxy")


def test_embedding_instruction_profiles_are_versioned_registry_data():
    assert embedding_instruction_profile("baseline_live_v0") == {
        "profile_name": "baseline_live_v0",
        "instruction": "given the user question, retrieve the most relevant information",
        "instruction_version": "qwen3-retrieval-query-v1",
    }
    universal = embedding_instruction_profile("universal")
    assert universal["instruction"].startswith("Given a question in everyday language")
    assert universal["instruction_version"] == (
        "embedding_instruction_registry.v1.universal"
    )
    with pytest.raises(RegistryError):
        embedding_instruction_profile("silent-unversioned-edit")


def test_unknown_ids_hard_error():
    with pytest.raises(RegistryError):
        domain("D99")
    with pytest.raises(RegistryError):
        superframe("MF99")
    with pytest.raises(RegistryError):
        motif("M99")
    with pytest.raises(RegistryError):
        admissible_superframes("M01", "NOT A STAGE")


def test_m12_qualifier_preserved():
    m = motif("M12")
    assert m["qualifier"] == "UNDER CONDITION"
    assert m["stages"] == ["INTERVENTION", "MEDIATOR", "OUTCOME"]
