from types import SimpleNamespace

import pytest

from models.schemas import IngestionConfig
from services.ingestion_service import IngestionService


@pytest.mark.asyncio
async def test_list_corpora_uses_cached_readiness_path(monkeypatch):
    service = IngestionService()
    refresh_modes: list[bool] = []

    async def fake_list_corpora(db, user_id=None):
        return [
            {
                "corpus_id": "corpus-1",
                "default_ingestion_config": IngestionConfig().model_dump(),
            }
        ]

    async def fake_refresh_corpus_counts(docs, *, refresh_readiness=True):
        refresh_modes.append(refresh_readiness)
        docs[0]["readiness"] = {"status": "fully_enriched"}

    monkeypatch.setattr(
        "services.storage.mongo_reader.list_corpora",
        fake_list_corpora,
    )
    monkeypatch.setattr(
        service,
        "_refresh_corpus_counts",
        fake_refresh_corpus_counts,
    )

    docs = await service.list_corpora(user_id="user-1")

    assert refresh_modes == [False]
    assert docs[0]["readiness"]["status"] == "fully_enriched"


@pytest.mark.asyncio
async def test_get_corpus_uses_cached_readiness_path(monkeypatch):
    service = IngestionService()
    refresh_modes: list[bool] = []

    async def fake_get_corpus(db, corpus_id):
        return {
            "corpus_id": corpus_id,
            "default_ingestion_config": IngestionConfig().model_dump(),
        }

    async def fake_refresh_corpus_counts(docs, *, refresh_readiness=True):
        refresh_modes.append(refresh_readiness)
        docs[0]["readiness"] = {"status": "queryable_partial"}

    monkeypatch.setattr(
        "services.storage.mongo_reader.get_corpus",
        fake_get_corpus,
    )
    monkeypatch.setattr(
        service,
        "_refresh_corpus_counts",
        fake_refresh_corpus_counts,
    )

    doc = await service.get_corpus("corpus-1")

    assert refresh_modes == [False]
    assert doc is not None
    assert doc["readiness"]["status"] == "queryable_partial"


@pytest.mark.asyncio
async def test_create_corpus_returns_materialized_readiness(monkeypatch):
    service = IngestionService()
    refreshed: list[str] = []

    async def fake_apply_global_summary_defaults(*, user_id, ingestion_config):
        return ingestion_config

    async def fake_upsert_corpus(db, corpus_doc):
        return None

    async def fake_ensure_corpus_retrieval_ready(**kwargs):
        return SimpleNamespace(ok=True, errors=[])

    async def fake_embed_and_upsert_schema_terms(*args, **kwargs):
        return None

    async def fake_refresh_corpus_counts(docs):
        refreshed.extend(str(doc["corpus_id"]) for doc in docs)
        for doc in docs:
            doc["ready_doc_count"] = 0
            doc["readiness"] = {
                "corpus_id": doc["corpus_id"],
                "status": "empty",
                "stale": False,
            }

    monkeypatch.setattr(
        service,
        "_apply_global_summary_defaults",
        fake_apply_global_summary_defaults,
    )
    monkeypatch.setattr(
        service,
        "_embed_and_upsert_schema_terms",
        fake_embed_and_upsert_schema_terms,
    )
    monkeypatch.setattr(service, "_refresh_corpus_counts", fake_refresh_corpus_counts)
    monkeypatch.setattr(
        "services.storage.mongo_writer.upsert_corpus",
        fake_upsert_corpus,
    )
    monkeypatch.setattr(
        "services.retrieval_readiness.ensure_corpus_retrieval_ready",
        fake_ensure_corpus_retrieval_ready,
    )

    doc = await service.create_corpus(
        name="readiness corpus",
        description=None,
        user_id="user-1",
        ingestion_config=IngestionConfig(),
    )

    assert refreshed == [doc["corpus_id"]]
    assert doc["readiness"]["corpus_id"] == doc["corpus_id"]
    assert doc["readiness"]["status"] == "empty"
    assert doc["readiness"]["stale"] is False


@pytest.mark.asyncio
async def test_update_corpus_returns_materialized_readiness(monkeypatch):
    service = IngestionService()
    refreshed: list[str] = []

    async def fake_update_corpus(db, corpus_id, updates):
        return {
            "corpus_id": corpus_id,
            "name": updates["name"],
            "description": None,
            "default_ingestion_config": IngestionConfig().model_dump(),
            "created_at": "2026-07-09T00:00:00Z",
            "updated_at": "2026-07-09T00:00:00Z",
            "doc_count": 3,
            "chunk_count": 30,
        }

    async def fake_refresh_corpus_counts(docs):
        refreshed.extend(str(doc["corpus_id"]) for doc in docs)
        for doc in docs:
            doc["ready_doc_count"] = 2
            doc["readiness"] = {
                "corpus_id": doc["corpus_id"],
                "status": "needs_repair",
                "stale": False,
            }

    monkeypatch.setattr(service, "_refresh_corpus_counts", fake_refresh_corpus_counts)
    monkeypatch.setattr(
        "services.storage.mongo_writer.update_corpus",
        fake_update_corpus,
    )

    doc = await service.update_corpus("corpus-1", {"name": "renamed"})

    assert refreshed == ["corpus-1"]
    assert doc["ready_doc_count"] == 2
    assert doc["readiness"] == {
        "corpus_id": "corpus-1",
        "status": "needs_repair",
        "stale": False,
    }
