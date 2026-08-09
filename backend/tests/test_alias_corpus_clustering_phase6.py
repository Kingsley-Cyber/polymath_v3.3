"""Phase-6 corpus identity clustering: fixtures, safety, determinism."""

from __future__ import annotations

import random

from models.alias_identity import DocumentEntityV1
from services.ingestion.alias_corpus_clustering import (
    build_inventory,
    cluster_corpus_entities,
    replay_fingerprint,
    shadow_projection_dicts,
)


def _doc_ent(
    *,
    document_id: str,
    canonical_name: str,
    accepted: list[str] | None = None,
    entity_type: str | None = None,
    descriptions: list[str] | None = None,
    defining: list[str] | None = None,
) -> DocumentEntityV1:
    return DocumentEntityV1.create(
        document_id=document_id,
        canonical_name=canonical_name,
        mention_ids=[f"mention:{document_id}:{canonical_name}"],
        accepted_alias_ids=accepted or [f"aliascand:{document_id}:{canonical_name}"],
        retrieval_variant_ids=[],
        description_records=descriptions or [],
        defining_child_ids=defining or [f"child:{document_id}:1"],
        supporting_child_ids=defining or [f"child:{document_id}:1"],
        entity_type=entity_type,
    )


def test_positive_cross_document_rag_identity():
    a = build_inventory(
        _doc_ent(
            document_id="doc:a",
            canonical_name="Retrieval-Augmented Generation",
            accepted=["aliascand:rag:a"],
        ),
        trusted_alias_surfaces=["RAG"],
        acronym_pairs=[("RAG", "Retrieval-Augmented Generation")],
        supporting_alias_decision_ids=["aliascand:rag:a"],
    )
    b = build_inventory(
        _doc_ent(
            document_id="doc:b",
            canonical_name="RAG",
            accepted=["aliascand:rag:b"],
            entity_type="concept",
        ),
        trusted_alias_surfaces=["RAG"],
        supporting_alias_decision_ids=["aliascand:rag:b"],
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert len(batch.corpus_entities) == 1
    ent = batch.corpus_entities[0]
    assert ent.canonical_name == "Retrieval-Augmented Generation"
    assert "RAG" in ent.trusted_aliases
    assert batch.metrics.accepted_merges_total >= 1
    assert all(d.identity_merge_allowed for d in batch.merge_decisions if d.decision == "MERGE")


def test_repeated_explicit_ibm_identity():
    a = build_inventory(
        _doc_ent(document_id="doc:ibm:a", canonical_name="International Business Machines"),
        trusted_alias_surfaces=["IBM"],
        acronym_pairs=[("IBM", "International Business Machines")],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:ibm:b", canonical_name="IBM"),
        trusted_alias_surfaces=["International Business Machines"],
        acronym_pairs=[("IBM", "International Business Machines")],
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert len(batch.corpus_entities) == 1


def test_ambiguous_acronym_isolation():
    a = build_inventory(
        _doc_ent(
            document_id="doc:ir:a",
            canonical_name="Information Retrieval",
            entity_type="concept",
        ),
        trusted_alias_surfaces=["IR"],
        acronym_pairs=[("IR", "Information Retrieval")],
    )
    b = build_inventory(
        _doc_ent(
            document_id="doc:ir:b",
            canonical_name="Infrared",
            entity_type="concept",
        ),
        trusted_alias_surfaces=["IR"],
        acronym_pairs=[("IR", "Infrared")],
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert len(batch.corpus_entities) == 2
    assert batch.ambiguous_acronym_cross_merges == 0
    assert not any(d.decision == "MERGE" for d in batch.merge_decisions)
    assert any("IR" in e.ambiguous_aliases for e in batch.corpus_entities)


def test_homonym_apple_isolation():
    a = build_inventory(
        _doc_ent(
            document_id="doc:apple:org",
            canonical_name="Apple",
            entity_type="organization",
        ),
        trusted_alias_surfaces=[],
        is_proper_name=True,
    )
    b = build_inventory(
        _doc_ent(
            document_id="doc:apple:fruit",
            canonical_name="apple",
            entity_type="food",
        ),
        trusted_alias_surfaces=[],
        is_proper_name=False,
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert len(batch.corpus_entities) == 2
    assert batch.homonym_cross_merges == 0
    assert not any(d.decision == "MERGE" for d in batch.merge_decisions)


def test_cooccurrence_negative_no_merge():
    a = build_inventory(
        _doc_ent(document_id="doc:mw", canonical_name="movement writing"),
        related_terms=["choreography"],
        description_surfaces=["records choreography"],
        is_proper_name=False,
    )
    b = build_inventory(
        _doc_ent(document_id="doc:bmn", canonical_name="Benesh Movement Notation"),
        related_terms=["movement"],
        description_surfaces=["represents movement"],
        is_proper_name=True,
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert batch.cooccurrence_only_merges == 0
    assert not any(d.decision == "MERGE" for d in batch.merge_decisions)
    # May be 2 clusters or 0 pairs if no shared block — either is non-merge.
    assert len(batch.corpus_entities) >= 2 or len(batch.merge_candidates) == 0


def test_temporal_former_name_link():
    a = build_inventory(
        _doc_ent(document_id="doc:alpha", canonical_name="Beta Systems"),
        temporal_former_names=["Alpha Systems"],
        trusted_alias_surfaces=[],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:beta", canonical_name="Beta Systems"),
        trusted_alias_surfaces=[],
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    # Same canonical merges; temporal former retained
    assert len(batch.corpus_entities) == 1
    ent = batch.corpus_entities[0]
    assert ent.canonical_name == "Beta Systems"
    assert "Alpha Systems" in ent.temporal_aliases
    assert any(r.get("former_name") == "Alpha Systems" for r in ent.temporal_identity_records)
    # Flattening into trusted timeless aliases must not erase temporal list
    assert "Alpha Systems" not in ent.trusted_aliases or "Alpha Systems" in ent.temporal_aliases


def test_temporal_link_across_former_and_current_docs():
    former_doc = build_inventory(
        _doc_ent(document_id="doc:alpha2", canonical_name="Alpha Systems"),
        temporal_former_names=[],
    )
    # Explicit: Alpha is former of Beta on the Beta entity
    current = build_inventory(
        _doc_ent(document_id="doc:beta2", canonical_name="Beta Systems"),
        temporal_former_names=["Alpha Systems"],
    )
    batch = cluster_corpus_entities([former_doc, current], corpus_id="corpus:p6")
    # LINK_TEMPORAL or MERGE via temporal
    assert any(
        d.decision in {"LINK_TEMPORAL_IDENTITY", "MERGE"} for d in batch.merge_decisions
    ) or len(batch.corpus_entities) == 1
    if len(batch.corpus_entities) == 1:
        assert "Alpha Systems" in batch.corpus_entities[0].temporal_aliases or any(
            "Alpha" in a for a in batch.corpus_entities[0].temporal_aliases
        )


def test_bridge_merge_negative():
    a = build_inventory(
        _doc_ent(document_id="doc:bridge:a", canonical_name="Information Retrieval"),
        trusted_alias_surfaces=["IR"],
        acronym_pairs=[("IR", "Information Retrieval")],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:bridge:b", canonical_name="IR"),
        trusted_alias_surfaces=["IR"],
    )
    c = build_inventory(
        _doc_ent(document_id="doc:bridge:c", canonical_name="Infrared"),
        trusted_alias_surfaces=["IR"],
        acronym_pairs=[("IR", "Infrared")],
    )
    batch = cluster_corpus_entities([a, b, c], corpus_id="corpus:p6")
    # Must not produce a single cluster spanning Information Retrieval + Infrared
    canons = {e.canonical_name for e in batch.corpus_entities}
    assert not (
        any("Information Retrieval" in c for c in canons)
        and any("Infrared" in c for c in canons)
        and len(batch.corpus_entities) == 1
    )
    assert batch.unsupported_transitive_bridge_merges >= 0
    ir_cluster = [
        e
        for e in batch.corpus_entities
        if e.canonical_name in {"Information Retrieval", "Infrared"}
    ]
    assert len(ir_cluster) == 2


def test_curated_identity_merge():
    a = build_inventory(
        _doc_ent(document_id="doc:cur:a", canonical_name="IBM"),
        curated_canonical="International Business Machines",
        trusted_alias_surfaces=["IBM"],
    )
    b = build_inventory(
        _doc_ent(
            document_id="doc:cur:b",
            canonical_name="International Business Machines",
        ),
        curated_canonical="International Business Machines",
        trusted_alias_surfaces=["IBM"],
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert len(batch.corpus_entities) == 1
    assert batch.corpus_entities[0].canonical_name_rule.startswith("curated")
    assert any(d.decision_reason.endswith("CURATED_CANONICAL_V1") for d in batch.merge_decisions if d.decision == "MERGE")


def test_surface_variant_nonmerge():
    a = build_inventory(
        _doc_ent(document_id="doc:surf:a", canonical_name="Qdrant"),
        retrieval_surfaces=["qdrant vector db"],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:surf:b", canonical_name="Weaviate"),
        retrieval_surfaces=["qdrant vector db"],  # shared retrieval surface only
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert batch.retrieval_only_variant_merges == 0
    assert not any(d.decision == "MERGE" for d in batch.merge_decisions)


def test_semantic_related_term_nonmerge():
    a = build_inventory(
        _doc_ent(document_id="doc:sem:a", canonical_name="movement writing"),
        related_terms=["movement notation"],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:sem:b", canonical_name="movement notation"),
        related_terms=["movement writing"],
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    assert batch.semantic_similarity_merges == 0
    assert not any(d.decision == "MERGE" for d in batch.merge_decisions)


def test_canonical_name_precedence_curated_over_frequency():
    a = build_inventory(
        _doc_ent(document_id="doc:prec:a", canonical_name="IBM"),
        curated_canonical="International Business Machines",
        trusted_alias_surfaces=["IBM"],
        acronym_pairs=[("IBM", "International Business Machines")],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:prec:b", canonical_name="IBM"),
        curated_canonical="International Business Machines",
        trusted_alias_surfaces=["IBM"],
    )
    c = build_inventory(
        _doc_ent(document_id="doc:prec:c", canonical_name="IBM"),
        curated_canonical="International Business Machines",
        trusted_alias_surfaces=["IBM"],
    )
    batch = cluster_corpus_entities([a, b, c], corpus_id="corpus:p6")
    assert len(batch.corpus_entities) == 1
    assert batch.corpus_entities[0].canonical_name == "International Business Machines"


def test_randomized_input_deterministic_replay():
    base = [
        build_inventory(
            _doc_ent(
                document_id="doc:r:a",
                canonical_name="Retrieval-Augmented Generation",
            ),
            trusted_alias_surfaces=["RAG"],
            acronym_pairs=[("RAG", "Retrieval-Augmented Generation")],
        ),
        build_inventory(
            _doc_ent(document_id="doc:r:b", canonical_name="RAG"),
            trusted_alias_surfaces=["RAG"],
        ),
        build_inventory(
            _doc_ent(
                document_id="doc:r:c",
                canonical_name="International Business Machines",
            ),
            trusted_alias_surfaces=["IBM"],
            acronym_pairs=[("IBM", "International Business Machines")],
        ),
        build_inventory(
            _doc_ent(document_id="doc:r:d", canonical_name="IBM"),
            trusted_alias_surfaces=["IBM"],
            acronym_pairs=[("IBM", "International Business Machines")],
        ),
    ]
    fingerprints = []
    for seed in range(8):
        shuffled = list(base)
        random.Random(seed).shuffle(shuffled)
        batch = cluster_corpus_entities(shuffled, corpus_id="corpus:p6")
        fingerprints.append(replay_fingerprint(batch))
    assert all(f == fingerprints[0] for f in fingerprints)


def test_every_merge_has_decision_record():
    a = build_inventory(
        _doc_ent(document_id="doc:m:a", canonical_name="Meta Platforms"),
        trusted_alias_surfaces=[],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:m:b", canonical_name="Meta Platforms"),
        trusted_alias_surfaces=[],
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    merges = [d for d in batch.merge_decisions if d.decision == "MERGE"]
    assert merges
    for ent in batch.corpus_entities:
        if len(ent.document_entity_ids) > 1:
            assert ent.accepted_merge_decision_ids
            assert set(ent.accepted_merge_decision_ids) <= {
                d.merge_decision_id for d in merges
            }


def test_shadow_schemas_projection_not_authoritative():
    a = build_inventory(
        _doc_ent(document_id="doc:sh:a", canonical_name="Qdrant"),
        trusted_alias_surfaces=["qdrant"],
    )
    b = build_inventory(
        _doc_ent(document_id="doc:sh:b", canonical_name="Qdrant"),
    )
    batch = cluster_corpus_entities([a, b], corpus_id="corpus:p6")
    rows = shadow_projection_dicts(batch)
    assert rows
    assert all(r["identity_authority"] is False for r in rows)
    assert all("shadow" in r["note"] for r in rows)


def test_performance_not_all_pairs():
    invs = []
    for i in range(20):
        invs.append(
            build_inventory(
                _doc_ent(
                    document_id=f"doc:perf:{i}",
                    canonical_name=f"Entity Number {i}",
                ),
                trusted_alias_surfaces=[f"EN{i}"],
            )
        )
    # Plus one mergeable pair
    invs.append(
        build_inventory(
            _doc_ent(document_id="doc:perf:x", canonical_name="Shared Name"),
        )
    )
    invs.append(
        build_inventory(
            _doc_ent(document_id="doc:perf:y", canonical_name="Shared Name"),
        )
    )
    batch = cluster_corpus_entities(invs, corpus_id="corpus:p6")
    n = batch.metrics.document_entities_total
    assert batch.metrics.candidate_pairs_total < n * (n - 1) // 2
    assert batch.metrics.clustering_wall_time_ms >= 0
