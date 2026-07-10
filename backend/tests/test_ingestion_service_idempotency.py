import pytest

from models.schemas import IngestJobResponse, IngestionConfig
from services.ingestion_service import (
    IngestionService,
    exact_source_duplicate_query,
)


def test_exact_source_duplicate_query_ignores_weak_filename_identity():
    query = exact_source_duplicate_query(
        corpus_id="corpus-1",
        source_identity={
            "source_kind": "filename",
            "source_key": "filename:notes.txt",
        },
    )

    assert query is None


def test_exact_source_duplicate_query_uses_strong_key_and_content_hash():
    query = exact_source_duplicate_query(
        corpus_id="corpus-1",
        source_identity={
            "source_kind": "url",
            "source_key": "url:https://example.com/book",
            "content_sha256": "abc123",
        },
    )

    assert query is not None
    rendered = str(query)
    assert "corpus-1" in rendered
    assert "url:https://example.com/book" not in rendered
    assert "source_identity.content_sha256" in rendered
    assert "content_sha256" in rendered
    assert "source_file_hash" in rendered
    assert "write_state.qdrant_written" in rendered
    assert "skipped_duplicate" in rendered


def test_exact_source_duplicate_query_uses_source_key_only_without_content_hash():
    query = exact_source_duplicate_query(
        corpus_id="corpus-1",
        source_identity={
            "source_kind": "url",
            "source_key": "url:https://example.com/book",
        },
    )

    assert query is not None
    rendered = str(query)
    assert "url:https://example.com/book" in rendered
    assert "source_identity.source_key" in rendered


class _FakeDocuments:
    def __init__(self, existing=None):
        self.existing = existing
        self.find_one_calls = []

    async def find_one(self, query, projection=None):
        self.find_one_calls.append((query, projection))
        return self.existing


class _FakeDb(dict):
    def __init__(self, existing=None):
        self.documents = _FakeDocuments(existing)
        super().__init__({"documents": self.documents})


@pytest.mark.asyncio
async def test_ingest_skips_exact_duplicate_before_worker(monkeypatch):
    service = IngestionService()
    service._db = _FakeDb(
        {
            "doc_id": "doc-existing",
            "filename": "original.md",
            "source_tier": "tier_b",
        }
    )
    seen_doc_ids = []
    seen_phases = []

    async def fail_worker(*_args, **_kwargs):
        raise AssertionError("exact duplicate should not reach run_ingest_job")

    async def on_doc_id(doc_id):
        seen_doc_ids.append(doc_id)

    async def on_phase(phase, details):
        seen_phases.append((phase, details))

    monkeypatch.setattr("services.ingestion.worker.run_ingest_job", fail_worker)

    result = await service.ingest(
        data=b"same source",
        filename="copy.md",
        corpus_id="corpus-1",
        user_id="user-1",
        ingestion_config=IngestionConfig(),
        model="",
        source_identity={
            "source_kind": "content_hash",
            "source_key": "sha256:abc123",
            "content_sha256": "abc123",
        },
        on_doc_id=on_doc_id,
        on_phase=on_phase,
    )

    assert result.status == "skipped_duplicate"
    assert result.doc_id == "doc-existing"
    assert result.source_tier == "tier_b"
    assert "Exact source duplicate skipped" in (result.error or "")
    assert seen_doc_ids == ["doc-existing"]
    assert seen_phases[0][0] == "skipped_duplicate"
    assert service._db.documents.find_one_calls


@pytest.mark.asyncio
async def test_ingest_duplicate_policy_allow_runs_worker(monkeypatch):
    service = IngestionService()
    service._db = _FakeDb(
        {
            "doc_id": "doc-existing",
            "filename": "original.md",
            "source_tier": "tier_b",
        }
    )
    calls = []

    async def fake_worker(**kwargs):
        calls.append(kwargs)
        return IngestJobResponse(
            job_id=kwargs["job_id"],
            doc_id="doc-new",
            corpus_id=kwargs["corpus_id"],
            filename=kwargs["filename"],
            source_tier="tier_b",
            status="done",
        )

    monkeypatch.setattr("services.ingestion.worker.run_ingest_job", fake_worker)

    result = await service.ingest(
        data=b"same source",
        filename="copy.md",
        corpus_id="corpus-1",
        user_id="user-1",
        ingestion_config=IngestionConfig(),
        model="",
        source_identity={
            "source_kind": "content_hash",
            "source_key": "sha256:abc123",
            "content_sha256": "abc123",
        },
        duplicate_policy="allow",
    )

    assert result.status == "done"
    assert result.doc_id == "doc-new"
    assert len(calls) == 1
    assert service._db.documents.find_one_calls == []
