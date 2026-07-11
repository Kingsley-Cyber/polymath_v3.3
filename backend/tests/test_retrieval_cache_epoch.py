import pytest

import services.retriever as retriever_module
from models.schemas import RetrievalResult, RetrievalTier, SourceChunk


@pytest.mark.asyncio
async def test_retrieval_cache_key_changes_with_durable_artifact_epoch(monkeypatch):
    retriever_module.invalidate_retrieval_cache()
    orchestrator = retriever_module.RetrieverOrchestrator()
    epoch = {"value": "2026-07-10T10:00:00Z"}
    calls = 0

    async def fake_epoch(corpus_ids):
        return (("corpus", epoch["value"]),)

    async def fake_retrieve_uncached(**kwargs):
        nonlocal calls
        calls += 1
        return RetrievalResult(
            chunks=[
                SourceChunk(
                    chunk_id=f"chunk-{calls}",
                    parent_id="parent",
                    doc_id="doc",
                    corpus_id="corpus",
                    text="evidence",
                    score=0.9,
                    source_tier="vector",
                )
            ],
            requested_tier=RetrievalTier.qdrant_only,
            effective_tier=RetrievalTier.qdrant_only,
        )

    monkeypatch.setattr(orchestrator, "_corpus_artifact_epoch", fake_epoch)
    monkeypatch.setattr(orchestrator, "_retrieve_uncached", fake_retrieve_uncached)
    kwargs = {
        "query": "query",
        "corpus_ids": ["corpus"],
        "retrieval_tier": RetrievalTier.qdrant_only,
        "collections": None,
        "rerank_enabled": False,
    }

    first = await orchestrator.retrieve(**kwargs)
    second = await orchestrator.retrieve(**kwargs)
    assert calls == 1
    assert first.chunks[0].chunk_id == second.chunks[0].chunk_id

    epoch["value"] = "2026-07-10T10:01:00Z"
    third = await orchestrator.retrieve(**kwargs)
    assert calls == 2
    assert third.chunks[0].chunk_id == "chunk-2"
