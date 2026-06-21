from __future__ import annotations

import pytest

from services import retrieval_readiness


class _AsyncCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    def __init__(self, rows):
        self.rows = list(rows)

    def find(self, *_args, **_kwargs):
        return _AsyncCursor(self.rows)

    async def find_one(self, query, *_args, **_kwargs):
        cid = query.get("corpus_id")
        for row in self.rows:
            if row.get("corpus_id") == cid:
                return row
        return None


class _Db:
    def __init__(self, corpora):
        self.corpora = _Collection(corpora)

    def __getitem__(self, name):
        if name == "corpora":
            return self.corpora
        raise KeyError(name)


@pytest.mark.asyncio
async def test_startup_repair_uses_each_corpus_dimension_and_neo4j_once(monkeypatch):
    qdrant_calls: list[dict] = []
    neo4j_calls = 0

    async def fake_ensure_collections(client, corpus_id, dim=1024, *, corpus_name=None):
        qdrant_calls.append(
            {
                "client": client,
                "corpus_id": corpus_id,
                "dim": dim,
                "corpus_name": corpus_name,
            }
        )

    async def fake_ensure_neo4j(driver, *, wait_timeout_s=15.0):
        nonlocal neo4j_calls
        neo4j_calls += 1
        return {"entity_name_ft": "ONLINE", "fact_text_ft": "ONLINE"}

    monkeypatch.setattr(
        retrieval_readiness,
        "ensure_collections_for_corpus",
        fake_ensure_collections,
    )
    monkeypatch.setattr(
        retrieval_readiness,
        "ensure_neo4j_retrieval_schema",
        fake_ensure_neo4j,
    )

    db = _Db(
        [
            {
                "corpus_id": "c1",
                "name": "Corpus One",
                "default_ingestion_config": {
                    "embedding_dimension": 384,
                    "target_qdrant_collections": ["naive", "hrag"],
                    "use_neo4j": False,
                },
            },
            {
                "corpus_id": "c2",
                "name": "Corpus Two",
                "default_ingestion_config": {
                    "embedding_dimension": 1024,
                    "target_qdrant_collections": ["naive", "hrag", "graph"],
                    "use_neo4j": True,
                },
            },
        ]
    )

    report = await retrieval_readiness.repair_retrieval_readiness_for_all_corpora(
        db=db,
        qdrant_client=object(),
        neo4j_driver=object(),
        neo4j_enabled=True,
        default_dim=1024,
    )

    assert report["scanned"] == 2
    assert report["ready"] == 2
    assert report["failed"] == 0
    assert neo4j_calls == 1
    assert report["reports"][1]["neo4j_required"] is True
    assert report["reports"][1]["qdrant_route_collections"] == {
        "naive": "corpus_c2_naive",
        "hrag": "corpus_c2_hrag",
        "graph": "corpus_c2_graph",
        "schemas": "corpus_c2_schemas",
    }
    assert [(c["corpus_id"], c["dim"]) for c in qdrant_calls] == [
        ("c1", 384),
        ("c2", 1024),
    ]
    assert qdrant_calls[0]["corpus_name"] == "Corpus One"


@pytest.mark.asyncio
async def test_ingest_readiness_reports_missing_required_neo4j(monkeypatch):
    async def fake_ensure_collections(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        retrieval_readiness,
        "ensure_collections_for_corpus",
        fake_ensure_collections,
    )
    db = _Db(
        [
            {
                "corpus_id": "c-graph",
                "name": "Graph Corpus",
                "default_ingestion_config": {
                    "embedding_dimension": 1024,
                    "target_qdrant_collections": ["naive", "hrag", "graph"],
                    "use_neo4j": True,
                },
            }
        ]
    )

    report = await retrieval_readiness.ensure_corpus_retrieval_ready(
        db=db,
        qdrant_client=object(),
        neo4j_driver=None,
        corpus_id="c-graph",
        neo4j_enabled=True,
    )

    assert report.qdrant_ready is True
    assert report.neo4j_ready is False
    assert not report.ok
    assert "driver unavailable" in "; ".join(report.errors)


@pytest.mark.asyncio
async def test_startup_repair_marks_graph_corpora_failed_when_neo4j_schema_fails(
    monkeypatch,
):
    neo4j_calls = 0

    async def fake_ensure_collections(*_args, **_kwargs):
        return None

    async def fake_ensure_neo4j(*_args, **_kwargs):
        nonlocal neo4j_calls
        neo4j_calls += 1
        raise RuntimeError("fulltext offline")

    monkeypatch.setattr(
        retrieval_readiness,
        "ensure_collections_for_corpus",
        fake_ensure_collections,
    )
    monkeypatch.setattr(
        retrieval_readiness,
        "ensure_neo4j_retrieval_schema",
        fake_ensure_neo4j,
    )
    db = _Db(
        [
            {
                "corpus_id": "c-graph",
                "name": "Graph Corpus",
                "default_ingestion_config": {
                    "embedding_dimension": 1024,
                    "use_neo4j": True,
                },
            }
        ]
    )

    report = await retrieval_readiness.repair_retrieval_readiness_for_all_corpora(
        db=db,
        qdrant_client=object(),
        neo4j_driver=object(),
        neo4j_enabled=True,
        default_dim=1024,
    )

    assert neo4j_calls == 1
    assert report["ready"] == 0
    assert report["failed"] == 1
    assert report["reports"][0]["qdrant_ready"] is True
    assert report["reports"][0]["neo4j_ready"] is False
    assert "fulltext offline" in "; ".join(report["reports"][0]["errors"])
