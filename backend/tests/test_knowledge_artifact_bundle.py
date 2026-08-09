"""Portable tests for KnowledgeArtifactBundleV1 recovery adapter."""

from __future__ import annotations

from services.ingestion.knowledge_bundle import (
    assertion_rows_from_bundle,
    bundle_from_extraction_row,
)


def _row(**overrides):
    base = {
        "corpus_id": "c1",
        "doc_id": "d1",
        "chunk_id": "ch1",
        "text": "Retrieval-Augmented Generation (RAG) uses external documents.",
        "entities": [
            {
                "canonical_name": "retrieval-augmented generation",
                "surface_form": "Retrieval-Augmented Generation",
                "entity_type": "Concept",
                "confidence": 0.9,
                "query_aliases": ["RAG"],
            }
        ],
        "relations": [
            {
                "subject": "retrieval-augmented generation",
                "predicate": "uses",
                "object": "external documents",
                "confidence": 0.8,
                "evidence_phrase": "uses external documents",
                "validation_status": "ACCEPT_HIGH",
                "object_kind": "entity",
            },
            {
                "subject": "x",
                "predicate": "maybe_related",
                "object": "y",
                "confidence": 0.1,
                "validation_status": "REVIEW_LOW",
            },
            {
                "subject": "a",
                "predicate": "related_to",
                "object": "b",
                "confidence": 0.1,
                "validation_status": "REJECT_NOISE",
            },
        ],
        "facts": [],
        "local_extraction": {
            "extractor_release": "relex_local.v1",
            "model_hash": "mh",
            "acceptance_policy_hash": "ap",
            "ontology_hash": "oh",
            "gate_decision_counts": {"ACCEPT_HIGH": 1, "REVIEW_LOW": 1, "REJECT_NOISE": 1},
            "contract": "polymath.relex_local.v1",
        },
        "extraction_contract_hash": "deadbeef",
    }
    base.update(overrides)
    return base


def test_bundle_splits_relation_lanes_and_hashes():
    b = bundle_from_extraction_row(_row(), corpus_generation="g1")
    assert b.schema_version == "knowledge_artifact_bundle.v1"
    assert b.identity.chunk_id == "ch1"
    assert len(b.entity_mentions) == 1
    assert b.entity_mentions[0].query_aliases == ["RAG"]
    assert len(b.accepted_relation_assertions) == 1
    assert len(b.review_relations) == 1
    assert len(b.rejected_relations) == 1
    assert b.qualified_facts == []
    assert b.releases.extractor_release == "relex_local.v1"
    assert b.audit.bundle_hash.startswith("sha256:")
    assert "RAG" in b.retrieval_surface_variants


def test_assertion_rows_ignore_empty_facts():
    b = bundle_from_extraction_row(_row())
    rows = assertion_rows_from_bundle(b)
    assert len(rows) == 1
    assert rows[0]["predicate"] == "uses"
    assert rows[0]["validation_status"] == "ACCEPT_HIGH"


def test_bundle_hash_stable():
    a = bundle_from_extraction_row(_row()).audit.bundle_hash
    b = bundle_from_extraction_row(_row()).audit.bundle_hash
    assert a == b
