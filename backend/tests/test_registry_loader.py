"""Owner-registry loader: cross-validation, resolver, and tamper-evidence.

The FROZEN_HASHES below pin the v1 owner-delivered registry snapshots. A hash
mismatch means a registry file was edited in place — which is forbidden: any
change ships as a NEW version file plus updated goldens, never a silent edit.
"""

from __future__ import annotations

import pytest

from models.registry_loader import (
    RegistryError,
    admissible_superframes,
    domain,
    domain_affinity_priors,
    is_controlled_predicate,
    is_entity_type,
    latent_budget,
    load_all,
    motif,
    registry_hashes,
    superframe,
)

FROZEN_HASHES = {
    "domain": "sha256:8e3c8c6e7d3b02bc689e7275a7a63877c0702b3b8d1601b59bfc2d1d0069dadb",
    "superframe": "sha256:dc386c970b57515a3e0e3371540ae21b1dfa33e71c961383efc1a124363c4d68",
    "affinity": "sha256:7894b6ecc0179e192d90a3c41b416de72da0a65dae5920f46e73cea25e2951da",
    "motif": "sha256:a1d79691bfa23628a3548a03f048c62f3f83df8e27b6fa3d80b774aa72cb7960",
    "vocab": "sha256:bd39855c608e2ce677587c63603eb51caef407fa422cb6575b549dcf68216aa5",
    "latent_policy": "sha256:1bd709eed623b1b6191a4563bd736e1a20544608b1ef94d9c5603f21d3debcd9",
    "binding": "sha256:2811ff011e38762b349b9d14a1f0352edc798b683adf8673106535bcd370c797",
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
        ("M02", "ATTENTION"), ("M03", "OUTCOME"), ("M05", "SCARCITY"),
        ("M06", "SOCIAL FEEDBACK"), ("M07", "RETENTION"),
        ("M10", "PROXY/HEURISTIC"), ("M11", "INTERDEPENDENCE"),
    ]
    for motif_id, stage in contested:
        assert len(admissible_superframes(motif_id, stage)) == 2, (motif_id, stage)


def test_affinity_priors_serve_only():
    priors = domain_affinity_priors("D16")
    assert priors == ["MF04", "MF08", "MF13", "MF15", "MF16"]


def test_vocab_membership():
    assert is_controlled_predicate("CAUSES")
    assert not is_controlled_predicate("VIBES_WITH")
    assert is_entity_type("BASELINE")
    assert not is_entity_type("THINGY")


def test_latent_budgets():
    assert latent_budget("parent_group") == {
        "raw_generated": [5, 12], "retained_distinct": [3, 8],
    }
    with pytest.raises(RegistryError):
        latent_budget("galaxy")


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
