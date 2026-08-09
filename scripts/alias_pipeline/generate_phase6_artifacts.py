#!/usr/bin/env python3
"""Generate Phase-6 shadow artifacts under data_eval/alias_pipeline/ (no prod writes)."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from models.alias_identity import DocumentEntityV1  # noqa: E402
from services.ingestion.alias_corpus_clustering import (  # noqa: E402
    build_inventory,
    cluster_corpus_entities,
    replay_fingerprint,
    shadow_projection_dicts,
)

OUT = ROOT / "data_eval" / "alias_pipeline"
CORPUS_ID = "corpus:alias_phase6_fixture"


def _ent(document_id: str, canonical_name: str, *, entity_type: str | None = None) -> DocumentEntityV1:
    return DocumentEntityV1.create(
        document_id=document_id,
        canonical_name=canonical_name,
        mention_ids=[f"mention:{document_id}"],
        accepted_alias_ids=[f"aliascand:{document_id}"],
        retrieval_variant_ids=[],
        description_records=[],
        defining_child_ids=[f"child:{document_id}:1"],
        supporting_child_ids=[f"child:{document_id}:1"],
        entity_type=entity_type,
    )


def fixture_inventories():
    return [
        # Positive RAG
        build_inventory(
            _ent("doc:rag:a", "Retrieval-Augmented Generation"),
            trusted_alias_surfaces=["RAG"],
            acronym_pairs=[("RAG", "Retrieval-Augmented Generation")],
        ),
        build_inventory(
            _ent("doc:rag:b", "RAG"),
            trusted_alias_surfaces=["RAG"],
        ),
        # IBM repeated explicit
        build_inventory(
            _ent("doc:ibm:a", "International Business Machines"),
            trusted_alias_surfaces=["IBM"],
            acronym_pairs=[("IBM", "International Business Machines")],
        ),
        build_inventory(
            _ent("doc:ibm:b", "IBM"),
            trusted_alias_surfaces=["IBM"],
            acronym_pairs=[("IBM", "International Business Machines")],
        ),
        # Ambiguous IR
        build_inventory(
            _ent("doc:ir:a", "Information Retrieval", entity_type="concept"),
            trusted_alias_surfaces=["IR"],
            acronym_pairs=[("IR", "Information Retrieval")],
        ),
        build_inventory(
            _ent("doc:ir:b", "Infrared", entity_type="concept"),
            trusted_alias_surfaces=["IR"],
            acronym_pairs=[("IR", "Infrared")],
        ),
        # Homonym Apple
        build_inventory(
            _ent("doc:apple:org", "Apple", entity_type="organization"),
        ),
        build_inventory(
            _ent("doc:apple:fruit", "apple", entity_type="food"),
            is_proper_name=False,
        ),
        # Co-occurrence negative
        build_inventory(
            _ent("doc:mw", "movement writing"),
            related_terms=["choreography"],
            is_proper_name=False,
        ),
        build_inventory(
            _ent("doc:bmn", "Benesh Movement Notation"),
            related_terms=["movement"],
        ),
        # Temporal
        build_inventory(
            _ent("doc:alpha", "Alpha Systems"),
        ),
        build_inventory(
            _ent("doc:beta", "Beta Systems"),
            temporal_former_names=["Alpha Systems"],
        ),
        # Bridge B (bare IR usage)
        build_inventory(
            _ent("doc:bridge:b", "IR"),
            trusted_alias_surfaces=["IR"],
        ),
        # Curated
        build_inventory(
            _ent("doc:cur:a", "IBM Corp"),
            curated_canonical="International Business Machines",
            trusted_alias_surfaces=["IBM"],
        ),
        build_inventory(
            _ent("doc:cur:b", "International Business Machines"),
            curated_canonical="International Business Machines",
        ),
        # Surface-only negative
        build_inventory(
            _ent("doc:surf:a", "Qdrant"),
            retrieval_surfaces=["vector database"],
        ),
        build_inventory(
            _ent("doc:surf:b", "Weaviate"),
            retrieval_surfaces=["vector database"],
        ),
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> int:
    invs = fixture_inventories()
    batch = cluster_corpus_entities(invs, corpus_id=CORPUS_ID)

    # Replay under shuffled orders
    fps = []
    for seed in range(5):
        shuffled = list(invs)
        random.Random(seed).shuffle(shuffled)
        fps.append(replay_fingerprint(cluster_corpus_entities(shuffled, corpus_id=CORPUS_ID)))
    replay_ok = all(f == fps[0] for f in fps)

    _write_jsonl(OUT / "corpus_merge_candidates.jsonl", batch.merge_candidates)
    _write_jsonl(
        OUT / "corpus_merge_decisions.jsonl",
        [d.model_dump() for d in batch.merge_decisions],
    )
    _write_jsonl(
        OUT / "corpus_entities.jsonl",
        [e.model_dump() for e in batch.corpus_entities],
    )
    _write_jsonl(OUT / "corpus_cluster_conflicts.jsonl", batch.conflicts)
    (OUT / "corpus_cluster_replay.json").write_text(
        json.dumps(
            {
                "replay_ok": replay_ok,
                "fingerprints": fps,
                "baseline": fps[0],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    metrics = {
        **batch.metrics.__dict__,
        "ambiguous_acronym_cross_merges": batch.ambiguous_acronym_cross_merges,
        "homonym_cross_merges": batch.homonym_cross_merges,
        "description_identity_merges": batch.description_identity_merges,
        "retrieval_only_variant_merges": batch.retrieval_only_variant_merges,
        "semantic_similarity_merges": batch.semantic_similarity_merges,
        "cooccurrence_only_merges": batch.cooccurrence_only_merges,
        "unsupported_transitive_bridge_merges": batch.unsupported_transitive_bridge_merges,
        "production_alias_records_mutated": False,
        "production_schemas_overwritten": False,
        "Fast_schema_expansion_activated": False,
    }
    (OUT / "corpus_cluster_metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (OUT / "corpus_shadow_schemas_projection.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in shadow_projection_dicts(batch)),
        encoding="utf-8",
    )

    # Acceptance matrix from fixture expectations
    canons = {e.canonical_name for e in batch.corpus_entities}
    false_merges = 0
    if any("Information Retrieval" in c and "Infrared" in "".join(canons) for c in canons):
        # single cluster containing both would be counted elsewhere
        pass
    ir_ents = [
        e
        for e in batch.corpus_entities
        if e.canonical_name in {"Information Retrieval", "Infrared"}
    ]
    if len(ir_ents) < 2:
        false_merges += 1
    apple_ents = [
        e for e in batch.corpus_entities if e.canonical_name.lower() == "apple"
    ]
    if len(apple_ents) < 2:
        false_merges += 1
    mw_bmn = [
        e
        for e in batch.corpus_entities
        if e.canonical_name in {"movement writing", "Benesh Movement Notation"}
    ]
    if len(mw_bmn) == 1:
        false_merges += 1

    rag_ents = [
        e
        for e in batch.corpus_entities
        if e.canonical_name == "Retrieval-Augmented Generation"
    ]
    acceptance = {
        "positive_rag_clusters": len(rag_ents),
        "positive_rag_trusted_alias_RAG": bool(
            rag_ents and "RAG" in rag_ents[0].trusted_aliases
        ),
        "ambiguous_ir_clusters": len(ir_ents),
        "homonym_apple_clusters": len(apple_ents),
        "cooccurrence_false_merge": len(mw_bmn) == 1,
        "false_merge_count": false_merges,
        "replay_ok": replay_ok,
        "every_merge_has_decision_record": all(
            e.accepted_merge_decision_ids
            for e in batch.corpus_entities
            if len(e.document_entity_ids) > 1
        ),
        "shadow_projection_rows": len(shadow_projection_dicts(batch)),
        "production_mutated": False,
    }
    (OUT / "phase6_acceptance_matrix.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"out": str(OUT), "acceptance": acceptance, "metrics": metrics}, indent=2))
    return 0 if replay_ok and false_merges == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
