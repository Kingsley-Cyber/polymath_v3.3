from routers.ingestion import _resolve_ingest_progress


DEEP_CONFIG = {
    "target_qdrant_collections": ["naive", "hrag", "graph"],
    "use_neo4j": True,
}


def test_deep_poll_status_waits_after_mongo_only():
    progress = _resolve_ingest_progress(
        {
            "ingestion_config": DEEP_CONFIG,
            "write_state": {
                "mongo_written": True,
                "qdrant_written": False,
                "neo4j_written": False,
                "verified": None,
            },
        },
        neo4j_enabled=True,
    )

    assert progress["status"] == "processing"
    assert progress["stage"] == "embedding"


def test_deep_poll_status_waits_for_neo4j_after_qdrant():
    progress = _resolve_ingest_progress(
        {
            "ingestion_config": DEEP_CONFIG,
            "write_state": {
                "mongo_written": True,
                "qdrant_written": True,
                "neo4j_written": False,
                "verified": None,
            },
        },
        neo4j_enabled=True,
    )

    assert progress["status"] == "processing"
    assert progress["stage"] == "graph_extracting"


def test_queryable_with_pending_graph_is_not_reported_failed():
    progress = _resolve_ingest_progress(
        {
            "ingest_stage": "queryable_with_pending_graph",
            "enrichment_pending_reason": "Queryable; graph pending.",
            "ingestion_config": DEEP_CONFIG,
            "write_state": {
                "mongo_written": True,
                "qdrant_written": True,
                "neo4j_written": False,
                "verified": False,
                "verify_errors": ["neo4j missing"],
            },
        },
        neo4j_enabled=True,
    )

    assert progress["status"] == "queryable_with_pending_graph"
    assert progress["stage"] == "queryable_with_pending_graph"
    assert progress["error"] == "Queryable; graph pending."


def test_deep_poll_status_waits_for_summary_indexing_after_qdrant():
    progress = _resolve_ingest_progress(
        {
            "ingestion_config": {
                **DEEP_CONFIG,
                "chunk_summarization": True,
            },
            "write_state": {
                "mongo_written": True,
                "qdrant_written": True,
                "summaries_indexed": False,
                "neo4j_written": False,
                "verified": None,
            },
        },
        neo4j_enabled=True,
    )

    assert progress["status"] == "processing"
    assert progress["stage"] == "summary_indexing"
    assert progress["summaries_indexed"] is False


def test_deep_poll_status_done_only_after_verify_passes():
    progress = _resolve_ingest_progress(
        {
            "ingestion_config": DEEP_CONFIG,
            "write_state": {
                "mongo_written": True,
                "qdrant_written": True,
                "neo4j_written": True,
                "verified": True,
            },
        },
        neo4j_enabled=True,
    )

    assert progress["status"] == "done"
    assert progress["stage"] == "verified"


def test_deep_poll_status_failed_when_verify_fails():
    progress = _resolve_ingest_progress(
        {
            "ingestion_config": DEEP_CONFIG,
            "write_state": {
                "mongo_written": True,
                "qdrant_written": True,
                "neo4j_written": True,
                "verified": False,
                "verify_errors": ["qdrant mismatch"],
            },
        },
        neo4j_enabled=True,
    )

    assert progress["status"] == "failed"
    assert progress["stage"] == "verify_failed"
    assert progress["verify_errors"] == ["qdrant mismatch"]
