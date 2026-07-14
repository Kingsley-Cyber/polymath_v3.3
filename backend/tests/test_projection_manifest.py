"""P2.5b ProjectionManifest: goldens, family validation, immutability semantics."""

from __future__ import annotations

import pytest

from models.projection_manifest import (
    MANIFEST_VERSION,
    NEO4J_PARTITIONS,
    QDRANT_FAMILIES,
    EmbeddingProfile,
    SearchCompat,
    make_manifest,
)


def _qdrant(**over):
    base = dict(
        store="qdrant", family="source_child", representation_role="primary_recall",
        source_schema_hashes={"chunk": "sha256:abc"}, payload_schema_hash="sha256:pay",
        embedding_profile=EmbeddingProfile(
            model_id="mlx-community/Qwen3-Embedding-0.6B-mxfp8", dims=1024,
            quantization="mxfp8", instruction_version="qwen3-retrieval-query-v1"),
        search_compat=SearchCompat(oversampling=2.0, rescore_with_full_vectors=True),
        recipe_version="proj.source_child.v1",
    )
    base.update(over)
    return make_manifest(**base)


GOLD_Q = "projm:20e7c60abe0b473ff09cda73eaf65d1685bee66c291514b8f2c9552208ebee15"
GOLD_G = "projm:25575ef267b38366f14d8d393b14b0a6ca61f7390ccb48738fe6fdad6919beb0"


def test_golden_manifest_ids():
    assert _qdrant().manifest_id == GOLD_Q
    g = make_manifest(store="neo4j", family="analogy_graph",
                      representation_role="graph_expansion",
                      source_schema_hashes={"analogy_card": "sha256:def"},
                      payload_schema_hash="sha256:node",
                      recipe_version="proj.analogy.v1")
    assert g.manifest_id == GOLD_G


def test_any_field_change_changes_identity():
    assert _qdrant().manifest_id != _qdrant(payload_schema_hash="sha256:other").manifest_id
    changed_instr = _qdrant(embedding_profile=EmbeddingProfile(
        model_id="mlx-community/Qwen3-Embedding-0.6B-mxfp8", dims=1024,
        quantization="mxfp8", instruction_version="universal.v1"))
    assert _qdrant().manifest_id != changed_instr.manifest_id  # instruction shifts identity


def test_rollback_predecessor_excluded_from_identity():
    a = _qdrant()
    b = _qdrant(rollback_predecessor=a.manifest_id)
    assert a.manifest_id == b.manifest_id  # lineage pointer is not identity


def test_owner_family_rulings_enforced():
    assert len(QDRANT_FAMILIES) == 8
    assert len(NEO4J_PARTITIONS) == 4
    with pytest.raises(ValueError):
        _qdrant(family="made_up_family")
    with pytest.raises(ValueError):
        make_manifest(store="neo4j", family="source_child",  # qdrant family on graph store
                      representation_role="x", source_schema_hashes={},
                      payload_schema_hash="sha256:x", recipe_version="v1")


def test_store_profile_constraints():
    with pytest.raises(ValueError):
        _qdrant(embedding_profile=None)  # qdrant requires a profile
    with pytest.raises(ValueError):
        make_manifest(store="neo4j", family="analogy_graph", representation_role="x",
                      source_schema_hashes={}, payload_schema_hash="sha256:x",
                      recipe_version="v1",
                      embedding_profile=EmbeddingProfile(
                          model_id="m", dims=1, quantization="float32",
                          instruction_version="v"))  # graph must not carry one


def test_document_side_always_raw():
    assert _qdrant().embedding_profile.document_side_instruction == "raw"


def test_version_frozen():
    assert MANIFEST_VERSION == "projection_manifest.v1"
