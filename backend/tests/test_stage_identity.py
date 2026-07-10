from services.ingestion.stage_identity import (
    document_stage_identity,
    embedding_model_hash,
    extraction_stage_identity,
    graph_promotion_stage_identity,
    normalized_text_hash,
    source_file_hash,
    summary_stage_identity,
)


def test_normalized_text_hash_ignores_whitespace_shape():
    assert normalized_text_hash("Alpha\n\nBeta") == normalized_text_hash(" Alpha Beta ")


def test_source_file_hash_prefers_nested_identity_content_hash():
    assert (
        source_file_hash({"source_identity": {"content_sha256": "abc"}, "content_sha256": "def"})
        == "abc"
    )


def test_embedding_model_hash_tracks_embedding_contract():
    first = embedding_model_hash(
        {
            "embedding_model_id": "embed-a",
            "ingestion_config": {"embedding_dimension": 1024, "embed_mode": "local"},
        }
    )
    second = embedding_model_hash(
        {
            "embedding_model_id": "embed-b",
            "ingestion_config": {"embedding_dimension": 1024, "embed_mode": "local"},
        }
    )

    assert first != second


def test_stage_identities_carry_expected_contract_hashes():
    doc = {
        "source_key": "sha256:abc",
        "source_identity": {"content_sha256": "abc"},
        "embedding_model_id": "embed-a",
    }
    chunk = {"text": "Alpha works.", "chunk_hash": "chunk-hash"}

    document_identity = document_stage_identity(
        doc=doc,
        pipeline_contract_hash="pipeline-contract",
    )
    extraction_identity = extraction_stage_identity(
        chunk=chunk,
        doc=doc,
        extraction_contract_hash="extract-contract",
    )
    summary_identity = summary_stage_identity(
        source={"text": "Parent text."},
        doc=doc,
        source_hash="summary-source",
        summary_contract_hash="summary-contract",
    )
    graph_identity = graph_promotion_stage_identity(
        doc=doc,
        extraction_artifact_ids=["artifact-b", "artifact-a", "artifact-a"],
        graph_contract_hash="graph-contract",
    )

    assert document_identity["source_file_hash"] == "abc"
    assert document_identity["pipeline_contract_hash"] == "pipeline-contract"
    assert extraction_identity["chunk_hash"] == "chunk-hash"
    assert extraction_identity["extraction_contract_hash"] == "extract-contract"
    assert summary_identity["source_hash"] == "summary-source"
    assert summary_identity["summary_contract_hash"] == "summary-contract"
    assert graph_identity["source_file_hash"] == "abc"
    assert graph_identity["extraction_artifact_ids"] == ["artifact-a", "artifact-b"]
    assert graph_identity["extraction_artifact_set_hash"]
    assert graph_identity["graph_contract_hash"] == "graph-contract"
