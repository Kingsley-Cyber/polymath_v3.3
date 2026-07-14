import pytest

from services import embedder


@pytest.mark.asyncio
async def test_embed_queries_batches_unique_cache_misses_once(monkeypatch):
    embedder._QUERY_EMBED_CACHE.clear()
    calls: list[list[str]] = []

    async def fake_embed_texts(texts, config):
        calls.append(list(texts))
        return [[float(index)] for index, _ in enumerate(texts)]

    monkeypatch.setattr(embedder._QUERY_BATCHER, "_embed_texts", fake_embed_texts)

    result = await embedder.embed_queries(["original", "lane", "original"])

    assert calls == [["original", "lane"]]
    assert result == [[0.0], [1.0], [0.0]]

    cached = await embedder.embed_queries(["lane", "original"])
    assert cached == [[1.0], [0.0]]
    assert len(calls) == 1


def test_query_cache_key_includes_qwen_profile_revision():
    baseline = {
        "embed_mode": "local",
        "embedding_model_id": "qwen3-embedding-0.6b-v1",
        "embedding_dimension": 1024,
        "query_instruction_profile": "baseline_live_v0",
    }
    universal = {**baseline, "query_instruction_profile": "universal"}
    before = embedder._query_cache_key("same raw query", baseline)
    after = embedder._query_cache_key("same raw query", universal)

    assert before != after
