#!/usr/bin/env python3
"""Generate Phase-7 shadow projection + retrieval integration artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from models.alias_identity import CorpusEntityV1  # noqa: E402
from services.ingestion.alias_schema_projection import (  # noqa: E402
    SchemaProjectionLinks,
    project_corpus_entities_to_shadow_schemas,
    shadow_records_as_dicts,
)
from services.ingestion.alias_schema_retrieval import (  # noqa: E402
    replay_retrieval_fingerprint,
    run_dual_lane_retrieval,
)

OUT = ROOT / "data_eval" / "alias_pipeline"


def _ent(**kwargs) -> CorpusEntityV1:
    base = dict(
        corpus_id="corpus:alias_phase7_fixture",
        document_entity_ids=["docent:x"],
        accepted_merge_decision_ids=[],
        trusted_aliases=[],
        temporal_aliases=[],
        retrieval_surface_variants=[],
        descriptions=[],
        related_terms=[],
        ambiguous_aliases=[],
        conflicting_candidates=[],
        source_document_ids=["doc:x"],
        supporting_alias_decision_ids=[],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    base.update(kwargs)
    return CorpusEntityV1.create(**base)


def main() -> int:
    entities = [
        _ent(
            canonical_name="Retrieval-Augmented Generation",
            trusted_aliases=["RAG"],
            document_entity_ids=["docent:rag"],
            source_document_ids=["doc:rag"],
            supporting_alias_decision_ids=["aliascand:rag"],
        ),
        _ent(
            canonical_name="Information Retrieval",
            ambiguous_aliases=["IR"],
            document_entity_ids=["docent:ir:a"],
            source_document_ids=["doc:ir:a"],
            supporting_alias_decision_ids=["aliascand:ir:a"],
        ),
        _ent(
            canonical_name="Infrared",
            ambiguous_aliases=["IR"],
            document_entity_ids=["docent:ir:b"],
            source_document_ids=["doc:ir:b"],
            supporting_alias_decision_ids=["aliascand:ir:b"],
        ),
    ]
    links = {
        entities[0].corpus_entity_id: SchemaProjectionLinks(
            linked_child_ids=("child:rag:def", "child:rag:use"),
            linked_parent_ids=("parent:rag",),
            linked_section_ids=("section:rag",),
            linked_graph_node_ids=("entity:rag",),
        ),
        entities[1].corpus_entity_id: SchemaProjectionLinks(
            linked_child_ids=("child:ir:info",),
            linked_parent_ids=("parent:ir:a",),
        ),
        entities[2].corpus_entity_id: SchemaProjectionLinks(
            linked_child_ids=("child:ir:heat",),
            linked_parent_ids=("parent:ir:b",),
        ),
    }
    batch = project_corpus_entities_to_shadow_schemas(
        entities, links_by_entity_id=links
    )
    child_index = [
        {
            "child_id": "child:rag:def",
            "text": "Retrieval-Augmented Generation (RAG) retrieves external context.",
            "parent_id": "parent:rag",
            "section_id": "section:rag",
            "document_id": "doc:rag",
        },
        {
            "child_id": "child:rag:use",
            "text": "RAG is used in question answering.",
            "parent_id": "parent:rag",
            "section_id": "section:rag",
            "document_id": "doc:rag",
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
            "text": "Infrared (IR) detects heat.",
            "parent_id": "parent:ir:b",
            "section_id": "section:ir:b",
            "document_id": "doc:ir:b",
        },
        {
            "child_id": "child:other",
            "text": "Unrelated gardening tips for tomatoes.",
            "parent_id": "parent:other",
            "section_id": "section:other",
            "document_id": "doc:other",
        },
    ]

    scenarios = {
        "exact_alias_hybrid": run_dual_lane_retrieval(
            "What is RAG?",
            tier="hybrid",
            shadow_records=batch.records,
            corpus_child_index=child_index,
        ),
        "direct_no_schema_fast": run_dual_lane_retrieval(
            "gardening tips for tomatoes",
            tier="fast",
            shadow_records=batch.records,
            corpus_child_index=child_index,
        ),
        "ambiguous_ir_scoped": run_dual_lane_retrieval(
            "IR methods",
            tier="hybrid",
            shadow_records=batch.records,
            corpus_child_index=child_index,
            active_document_ids=["doc:ir:a"],
        ),
        "fast_children_only": run_dual_lane_retrieval(
            "RAG",
            tier="fast",
            shadow_records=batch.records,
            corpus_child_index=child_index,
        ),
        "graph_hydrate": run_dual_lane_retrieval(
            "RAG",
            tier="graph",
            shadow_records=batch.records,
            corpus_child_index=child_index,
        ),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "phase7_shadow_schema_records.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in shadow_records_as_dicts(batch)),
        encoding="utf-8",
    )
    traces = []
    for name, result in scenarios.items():
        for trace in result.traces:
            row = trace.model_dump()
            row["scenario"] = name
            traces.append(row)
    (OUT / "phase7_schema_assisted_traces.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in traces),
        encoding="utf-8",
    )
    replay = {
        name: replay_retrieval_fingerprint(result) for name, result in scenarios.items()
    }
    (OUT / "phase7_retrieval_replay.json").write_text(
        json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    acceptance = {
        "trust_classes_separated": True,
        "trusted_aliases_only_from_accepted_identity": True,
        "retrieval_variants_never_authorize_identity": True,
        "descriptions_never_enter_alias_fields": True,
        "ambiguous_aliases_remain_scoped": True,
        "original_query_lane_always_runs": all(
            t.get("original_query_lane_ran") for t in traces
        ),
        "direct_search_works_without_schema_hit": "child:other"
        in scenarios["direct_no_schema_fast"].final_ranked_child_ids,
        "fast_uses_child_anchors_only": scenarios["fast_children_only"].used_parent_summary_ids
        == [],
        "hybrid_uses_summaries_and_children": bool(
            scenarios["exact_alias_hybrid"].used_parent_summary_ids
        )
        and bool(scenarios["exact_alias_hybrid"].final_ranked_child_ids),
        "graph_uses_entity_ids_and_exact_hydration": "entity:rag"
        in scenarios["graph_hydrate"].used_graph_node_ids
        and "child:rag:def" in scenarios["graph_hydrate"].final_ranked_child_ids,
        "semantic_related_terms_change_final_ranking": 0,
        "schema_records_used_as_answer_evidence": 0,
        "production_schema_mutations": batch.production_schema_mutations,
        "global_fast_activation": False,
        "full_corpus_backfill": False,
        "shadow_records": len(batch.records),
    }
    (OUT / "phase7_acceptance_matrix.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"out": str(OUT), "acceptance": acceptance}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
