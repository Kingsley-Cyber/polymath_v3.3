"""Asserting tests for A1 connection pooling + embed-config cache.

Run inside the backend image (deps available):
    docker run --rm -e LITELLM_MASTER_KEY=test -e AUTH_SECRET_KEY=test \
      -e DEFAULT_ADMIN_PASSWORD=test -v $PWD/backend:/app -w /app \
      --entrypoint python polymath_v33-backend tests/test_connection_pooling.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import httpx  # noqa: E402

from services import embedder, reranker  # noqa: E402


def test_embedder_http_client_is_a_reused_singleton():
    c1 = embedder._get_local_http_client()
    c2 = embedder._get_local_http_client()
    assert c1 is c2, "embedder client should be reused, not recreated per call"
    assert isinstance(c1, httpx.AsyncClient)


def test_reranker_http_client_is_a_reused_singleton():
    # Instance-scoped on the singleton service: pools in production, isolated per
    # fresh RerankerService in tests.
    svc = reranker.RerankerService()
    c1 = svc._get_http_client(4.0)
    c2 = svc._get_http_client(4.0)
    assert c1 is c2, "reranker client should be reused, not recreated per call"
    assert isinstance(c1, httpx.AsyncClient)
    # a different service instance gets its OWN client (test isolation guarantee)
    other = reranker.RerankerService()
    assert other._get_http_client(4.0) is not c1


def test_embed_config_for_query_is_cached_by_corpus():
    import services.conversation as conv
    import services.retriever as ret
    from services.retriever import retriever_orchestrator

    calls = {"n": 0}

    class _FakeColl:
        async def find_one(self, *a, **k):
            calls["n"] += 1
            return {"default_ingestion_config": {"provider": "local"}}

    class _FakeDB:
        def __getitem__(self, name):
            return _FakeColl()

    ret._EMBED_CONFIG_CACHE.clear()
    old_db = conv.conversation_service._db
    conv.conversation_service._db = _FakeDB()
    try:
        cid = ["corpus-xyz"]
        r1 = asyncio.run(retriever_orchestrator._embedding_config_for_query(cid))
        r2 = asyncio.run(retriever_orchestrator._embedding_config_for_query(cid))
        assert r1 == {"provider": "local"}, r1
        assert r2 == r1
        # Second call must be served from cache, not a second Mongo find_one.
        assert calls["n"] == 1, f"expected 1 Mongo lookup, got {calls['n']}"
    finally:
        conv.conversation_service._db = old_db
        ret._EMBED_CONFIG_CACHE.clear()


def test_multi_corpus_config_query_skips_lookup_entirely():
    from services.retriever import retriever_orchestrator

    # >1 corpus or none -> returns None without touching Mongo (existing contract)
    assert asyncio.run(retriever_orchestrator._embedding_config_for_query(["a", "b"])) is None
    assert asyncio.run(retriever_orchestrator._embedding_config_for_query(None)) is None


def test_query_embedding_is_cached():
    calls = {"n": 0}

    async def fake_local(texts, dim):
        calls["n"] += 1
        return [[0.1, 0.2, 0.3]]

    embedder._QUERY_EMBED_CACHE.clear()
    old = embedder._embed_batch_local
    embedder._embed_batch_local = fake_local
    try:
        v1 = asyncio.run(embedder.embed_query("hello world"))
        v2 = asyncio.run(embedder.embed_query("hello world"))
        assert v1 == [0.1, 0.2, 0.3]
        assert v2 == v1
        assert calls["n"] == 1, f"second embed should hit cache, got {calls['n']} sidecar calls"
        # distinct text must miss and re-embed
        asyncio.run(embedder.embed_query("a different query"))
        assert calls["n"] == 2
    finally:
        embedder._embed_batch_local = old
        embedder._QUERY_EMBED_CACHE.clear()


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
