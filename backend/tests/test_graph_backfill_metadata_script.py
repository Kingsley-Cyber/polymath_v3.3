from scripts import backfill_relates_to_support_metadata as script
from scripts import backfill_qdrant_graph_defaults as qdrant_defaults
from scripts import backfill_relation_support_records as support_script


def test_backfill_script_materializes_relation_and_entity_metadata():
    assert "r.support_count" in script.BACKFILL_QUERY
    assert "r.promote_version" in script.BACKFILL_QUERY

    entity_query = script.ENTITY_BACKFILL_QUERY
    assert "e.source_corpora" in entity_query
    assert "e.corpus_count" in entity_query
    assert "e.mentions" in entity_query
    assert "e.graph_degree" in entity_query
    assert "e.generic_entity" in entity_query
    assert "e.graph_expansion_allowed" in entity_query
    assert "entity_name IN $generic_terms" in entity_query
    assert "model" in script.GENERIC_ENTITY_TERMS
    assert "system" in script.GENERIC_ENTITY_TERMS


def test_support_record_backfill_reconstructs_relation_rows_from_staging():
    rows = support_script._relation_rows_from_extraction(
        {
            "schema_version": "polymath.extract.v1",
            "chunk_id": "chunk-1",
            "doc_id": "doc-1",
            "relations": [
                {
                    "subject": "Lambda",
                    "predicate": "uses",
                    "object": "S3",
                    "confidence": 0.88,
                    "evidence_phrase": "Lambda uses S3 events",
                },
                {"subject": "", "predicate": "uses", "object": "ignored"},
            ],
        }
    )

    assert len(rows) == 1
    assert rows[0]["subject_id"] == "entity:lambda"
    assert rows[0]["object_id"] == "entity:s3"
    assert rows[0]["predicate"] == "uses"
    assert rows[0]["chunk_id"] == "chunk-1"
    assert rows[0]["evidence_phrase"] == "Lambda uses S3 events"


def test_support_record_backfill_candidates_require_staged_extraction():
    pipeline = support_script._candidate_docs_pipeline(batch_size=10, corpus_id="corpus-a")

    assert pipeline[0] == {"$match": {"status": "ok", "corpus_id": "corpus-a"}}
    assert pipeline[1] == {"$group": {"_id": {"corpus_id": "$corpus_id", "doc_id": "$doc_id"}}}
    lookup_match = pipeline[2]["$lookup"]["pipeline"][0]["$match"]
    assert lookup_match["relation_support_backfilled_at"] == {"$exists": False}
    assert lookup_match["write_state.neo4j_written"] is True
    assert {"status": {"$exists": False}} in lookup_match["$or"]
    assert {"status": "active"} in lookup_match["$or"]


def test_qdrant_graph_defaults_patch_only_missing_keys():
    patch = qdrant_defaults._missing_graph_defaults(
        {
            "entity_ids": ["entity:gliner"],
            "has_relations": True,
            "relation_predicates": None,
        }
    )

    assert patch["concepts"] == []
    assert patch["relation_predicates"] == []
    assert "has_relations" not in patch
    assert "entity_ids" not in patch
