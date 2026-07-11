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
