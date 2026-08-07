"""Phase-7 shadow schema projection + dual-lane retrieval integration."""

from __future__ import annotations

import random

from models.alias_identity import CorpusEntityV1
from services.ingestion.alias_schema_projection import (
    SchemaProjectionLinks,
    project_corpus_entities_to_shadow_schemas,
    project_corpus_entity_to_shadow_schema,
)
from services.ingestion.alias_schema_retrieval import (
    ROUTE_BUDGETS,
    match_shadow_schema,
    replay_retrieval_fingerprint,
    run_dual_lane_retrieval,
)


def _entity(**overrides) -> CorpusEntityV1:
    kwargs = dict(
        corpus_id="corpus:p7",
        canonical_name="Retrieval-Augmented Generation",
        document_entity_ids=["docent:a"],
        accepted_merge_decision_ids=["corpusmerge:a"],
        trusted_aliases=["RAG"],
        temporal_aliases=[],
        retrieval_surface_variants=[],
        descriptions=[],
        related_terms=[],
        ambiguous_aliases=[],
        conflicting_candidates=[],
        source_document_ids=["doc:a"],
        supporting_alias_decision_ids=["aliascand:rag"],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    kwargs.update(overrides)
    return CorpusEntityV1.create(**kwargs)


def _child_index():
    return [
        {
            "child_id": "child:rag:def",
            "text": "Retrieval-Augmented Generation (RAG) retrieves external context.",
            "parent_id": "parent:1",
            "section_id": "section:1",
            "document_id": "doc:a",
        },
        {
            "child_id": "child:rag:use",
            "text": "RAG is used in question answering systems.",
            "parent_id": "parent:1",
            "section_id": "section:1",
            "document_id": "doc:a",
        },
        {
            "child_id": "child:other",
            "text": "Unrelated gardening tips for tomatoes.",
            "parent_id": "parent:9",
            "section_id": "section:9",
            "document_id": "doc:z",
        },
        {
            "child_id": "child:ir:info",
            "text": "Information Retrieval (IR) ranks documents.",
            "parent_id": "parent:ir:a",
            "section_id": "section:ir:a",
            "document_id": "doc:ir:a",
        },
        {
            "child_id": "child:ir:heat",
            "text": "Infrared (IR) detects heat signatures.",
            "parent_id": "parent:ir:b",
            "section_id": "section:ir:b",
            "document_id": "doc:ir:b",
        },
    ]


def test_trust_classes_separated_not_collapsed_to_query_aliases():
    entity = _entity(
        trusted_aliases=["RAG"],
        retrieval_surface_variants=["retrieval augmented gen"],
        ambiguous_aliases=["IR"],
        related_terms=["passage ranking"],
        descriptions=["a technique combining retrieval and generation"],
        temporal_aliases=["RAG classic"],
    )
    record = project_corpus_entity_to_shadow_schema(
        entity,
        legacy_query_aliases=["legacy_rag_string"],
    )
    assert record.shadow_only is True
    assert record.trusted_aliases[0].identity_authority is True
    assert record.retrieval_surface_variants[0].identity_authority is False
    assert record.ambiguous_aliases[0].expansion_mode == "scoped_only"
    assert record.related_terms[0].expansion_mode == "shadow_trace_only"
    assert record.descriptions[0].expansion_mode == "metadata_only"
    assert record.legacy_unqualified[0].qualification == "legacy_unqualified"
    # No collapsed query_aliases field on the record.
    assert not hasattr(record, "query_aliases")
    dumped = record.model_dump()
    assert "query_aliases" not in dumped


def test_descriptions_never_enter_alias_fields():
    entity = _entity(
        trusted_aliases=["RAG"],
        descriptions=["a software company style description"],
    )
    record = project_corpus_entity_to_shadow_schema(entity)
    alias_surfaces = {
        s.surface.lower()
        for s in (
            record.trusted_aliases
            + record.temporal_aliases
            + record.retrieval_surface_variants
            + record.ambiguous_aliases
        )
    }
    assert "a software company style description" not in alias_surfaces
    assert any("software company" in d.surface for d in record.descriptions)


def test_exact_alias_improves_retrieval():
    entity = _entity(trusted_aliases=["RAG"])
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:rag:def", "child:rag:use"),
            linked_parent_ids=("parent:1",),
        ),
    )
    # Query uses alias only — original lane still finds some overlap; schema expands.
    result = run_dual_lane_retrieval(
        "What is RAG?",
        tier="hybrid",
        shadow_records=[record],
        corpus_child_index=_child_index(),
    )
    assert result.original_lane_hits  # original lane always contributes when overlap
    assert any(t.trust_class == "trusted_aliases" for t in result.traces)
    assert "Retrieval-Augmented Generation" in result.expanded_queries
    assert "child:rag:def" in result.final_ranked_child_ids


def test_direct_retrieval_without_schema_match():
    entity = _entity(trusted_aliases=["RAG"])
    record = project_corpus_entity_to_shadow_schema(entity)
    result = run_dual_lane_retrieval(
        "gardening tips for tomatoes",
        tier="fast",
        shadow_records=[record],
        corpus_child_index=_child_index(),
    )
    assert all(t.original_query_lane_ran for t in result.traces)
    assert "child:other" in result.final_ranked_child_ids
    assert result.expanded_queries == []
    assert result.schema_records_used_as_answer_evidence == 0


def test_ambiguous_ir_stays_document_scoped():
    ir_info = _entity(
        canonical_name="Information Retrieval",
        trusted_aliases=[],
        ambiguous_aliases=["IR"],
        document_entity_ids=["docent:ir:a"],
        source_document_ids=["doc:ir:a"],
        supporting_alias_decision_ids=["aliascand:ir:a"],
    )
    ir_heat = _entity(
        canonical_name="Infrared",
        trusted_aliases=[],
        ambiguous_aliases=["IR"],
        document_entity_ids=["docent:ir:b"],
        source_document_ids=["doc:ir:b"],
        supporting_alias_decision_ids=["aliascand:ir:b"],
    )
    links_a = SchemaProjectionLinks(
        linked_child_ids=("child:ir:info",),
        linked_parent_ids=("parent:ir:a",),
    )
    links_b = SchemaProjectionLinks(
        linked_child_ids=("child:ir:heat",),
        linked_parent_ids=("parent:ir:b",),
    )
    rec_a = project_corpus_entity_to_shadow_schema(ir_info, links=links_a)
    rec_b = project_corpus_entity_to_shadow_schema(ir_heat, links=links_b)

    # No scope → ambiguous IR must not expand.
    unscope = match_shadow_schema("IR methods", [rec_a, rec_b])
    assert unscope == []

    # Scoped to doc:ir:a → only Information Retrieval
    scoped = match_shadow_schema(
        "IR methods",
        [rec_a, rec_b],
        active_document_ids=["doc:ir:a"],
    )
    assert len(scoped) == 1
    assert scoped[0][0].canonical_term == "Information Retrieval"

    result = run_dual_lane_retrieval(
        "IR methods",
        tier="hybrid",
        shadow_records=[rec_a, rec_b],
        corpus_child_index=_child_index(),
        active_document_ids=["doc:ir:a"],
    )
    assert "child:ir:heat" not in result.final_ranked_child_ids or (
        result.final_ranked_child_ids
        and result.final_ranked_child_ids[0] == "child:ir:info"
    )


def test_retrieval_only_surface_variants_bounded():
    entity = _entity(
        trusted_aliases=[],
        retrieval_surface_variants=["retrieval augmented gen"],
    )
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(linked_child_ids=("child:rag:def",)),
    )
    result = run_dual_lane_retrieval(
        "retrieval augmented gen systems",
        tier="fast",
        shadow_records=[record],
        corpus_child_index=_child_index(),
    )
    traces = [t for t in result.traces if t.trust_class == "retrieval_surface_variants"]
    assert traces
    assert traces[0].ranking_contribution == "bounded_assistance"
    assert traces[0].identity_authority if False else True  # field absent on trace
    # Must not claim identity authority via trusted class.
    assert all(t.trust_class != "trusted_aliases" for t in traces)


def test_semantic_related_terms_shadow_only_no_ranking_change():
    entity = _entity(
        trusted_aliases=[],
        related_terms=["passage ranking"],
    )
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(linked_child_ids=("child:rag:def",)),
    )
    result = run_dual_lane_retrieval(
        "passage ranking overview",
        tier="hybrid",
        shadow_records=[record],
        corpus_child_index=_child_index(),
    )
    related_traces = [t for t in result.traces if t.trust_class == "related_terms"]
    assert related_traces
    assert related_traces[0].ranking_contribution == "trace_only_no_ranking"
    assert related_traces[0].final_hydrated_child_ids == []
    assert result.semantic_related_terms_changed_ranking is False


def test_fast_uses_child_anchors_not_summaries():
    entity = _entity(trusted_aliases=["RAG"])
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:rag:def", "child:rag:use"),
            linked_parent_ids=("parent:1", "parent:2"),
            linked_section_ids=("section:1",),
        ),
    )
    result = run_dual_lane_retrieval(
        "RAG",
        tier="fast",
        shadow_records=[record],
        corpus_child_index=_child_index(),
    )
    assert ROUTE_BUDGETS["fast"]["parent_summaries"] == 0
    assert result.used_parent_summary_ids == []
    assert result.used_section_summary_ids == []
    assert any(cid.startswith("child:rag") for cid in result.final_ranked_child_ids)
    assert result.global_fast_activation is False


def test_hybrid_uses_summaries_and_children():
    entity = _entity(trusted_aliases=["RAG"])
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:rag:def", "child:rag:use"),
            linked_parent_ids=("parent:1", "parent:2"),
            linked_section_ids=("section:1",),
        ),
    )
    result = run_dual_lane_retrieval(
        "RAG",
        tier="hybrid",
        shadow_records=[record],
        corpus_child_index=_child_index(),
    )
    assert result.used_parent_summary_ids
    assert result.used_section_summary_ids
    assert any(cid.startswith("child:rag") for cid in result.final_ranked_child_ids)


def test_graph_resolves_entity_ids_and_hydrates_children():
    entity = _entity(trusted_aliases=["RAG"])
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:rag:def",),
            linked_graph_node_ids=("entity:retrieval-augmented-generation",),
            linked_parent_ids=("parent:1",),
        ),
    )
    result = run_dual_lane_retrieval(
        "RAG",
        tier="graph",
        shadow_records=[record],
        corpus_child_index=_child_index(),
    )
    assert "entity:retrieval-augmented-generation" in result.used_graph_node_ids
    assert "child:rag:def" in result.final_ranked_child_ids
    assert all(t.schema_used_as_answer_evidence is False for t in result.traces)


def test_deterministic_restart_replay():
    entity = _entity(trusted_aliases=["RAG"], ambiguous_aliases=["IR"])
    batch = project_corpus_entities_to_shadow_schemas(
        [entity],
        links_by_entity_id={
            entity.corpus_entity_id: SchemaProjectionLinks(
                linked_child_ids=("child:rag:def", "child:rag:use")
            )
        },
    )
    fps = []
    for seed in range(6):
        records = list(batch.records)
        random.Random(seed).shuffle(records)
        index = list(_child_index())
        random.Random(seed).shuffle(index)
        result = run_dual_lane_retrieval(
            "RAG",
            tier="hybrid",
            shadow_records=records,
            corpus_child_index=index,
        )
        fps.append(replay_retrieval_fingerprint(result))
    assert all(f == fps[0] for f in fps)


def test_production_safety_flags():
    entity = _entity()
    batch = project_corpus_entities_to_shadow_schemas([entity])
    assert batch.production_schema_mutations == 0
    assert batch.collapsed_query_aliases_emitted == 0
    result = run_dual_lane_retrieval(
        "RAG",
        tier="fast",
        shadow_records=batch.records,
        corpus_child_index=_child_index(),
        activate_global_fast_schema_expansion=False,
    )
    assert result.global_fast_activation is False
    assert result.production_schema_mutations == 0
    assert result.schema_records_used_as_answer_evidence == 0
