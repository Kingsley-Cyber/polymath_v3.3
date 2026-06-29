"""
Registers the `integration` marker and auto-skips integration tests unless
the user opts in with `pytest -m integration`.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: live smoke test against the running docker-compose "
        "stack (Mongo + Qdrant + Neo4j + LLM). Skipped by default; run with "
        "`pytest -m integration` to include.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    explicit = (config.getoption("-m") or "").strip()
    # User explicitly asked for integration → run them (and only them by marker rules).
    if "integration" in explicit:
        return
    skip = pytest.mark.skip(reason="integration test; run with `-m integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _clear_rag_caches():
    """Clear in-process RAG caches between tests so a cached retrieval/embedding
    result from one test cannot leak into another. These caches are correct in
    production (identical queries SHOULD reuse results); the isolation matters
    only for tests that re-run the same query params expecting a fresh pipeline."""

    def _clear() -> None:
        try:
            import services.retriever as _ret

            _ret._RETRIEVAL_CACHE.clear()
            _ret._EMBED_CONFIG_CACHE.clear()
        except Exception:
            pass
        try:
            import services.embedder as _emb

            _emb._QUERY_EMBED_CACHE.clear()
        except Exception:
            pass

    _clear()
    yield
    _clear()
