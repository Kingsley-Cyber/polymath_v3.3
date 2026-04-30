import pytest

from services.ingestion_service import IngestionService


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return list(self._rows)


class _Collection:
    def __init__(self, rows=None, count=0):
        self._rows = rows or []
        self._count = count

    def find(self, *_args, **_kwargs):
        return _Cursor(self._rows)

    async def count_documents(self, *_args, **_kwargs):
        return self._count


class _AuditDB:
    def __init__(self, docs, chunk_count):
        self._collections = {
            "documents": _Collection(docs),
            "chunks": _Collection(count=chunk_count),
        }

    def __getitem__(self, name):
        return self._collections[name]


async def _audit_for(doc, chunk_count=10):
    service = IngestionService()
    service._db = _AuditDB([doc], chunk_count)
    return await service.get_ingestion_audit("corp1")


def _doc(metrics, *, failures=None, verified=True):
    return {
        "doc_id": "doc1",
        "filename": "doc.md",
        "ghost_b_failures": failures or [],
        "ghost_b_staging": [{}] * int(metrics.get("extracted_chunks") or 0),
        "ghost_b_metrics": metrics,
        "write_state": {"verified": verified, "warnings": []},
    }


@pytest.mark.asyncio
async def test_ingestion_audit_ready_classification():
    audit = await _audit_for(
        _doc(
            {
                "extracted_chunks": 10,
                "failed_chunk_count": 0,
                "ghost_b_success_rate": 1.0,
                "relation_count": 10,
                "related_to_count": 1,
                "related_to_ratio": 0.1,
                "predicate_confidence_avg": 0.91,
            }
        )
    )

    assert audit["readiness"] == "ready"
    assert audit["totals"]["ghost_b_success_rate"] == 1.0
    assert audit["document_metrics"][0]["readiness"] == "ready"


@pytest.mark.asyncio
async def test_ingestion_audit_needs_backfill_classification():
    audit = await _audit_for(
        _doc(
            {
                "extracted_chunks": 9,
                "failed_chunk_count": 1,
                "ghost_b_success_rate": 0.9,
                "relation_count": 10,
                "related_to_ratio": 0.1,
            },
            failures=[{"chunk_id": "c-failed"}],
        )
    )

    assert audit["readiness"] == "needs_backfill"
    assert audit["totals"]["failed_chunk_count"] == 1
    assert audit["partial_docs"][0]["readiness"] == "needs_backfill"


@pytest.mark.asyncio
async def test_ingestion_audit_schema_review_classification():
    audit = await _audit_for(
        _doc(
            {
                "extracted_chunks": 10,
                "failed_chunk_count": 0,
                "ghost_b_success_rate": 1.0,
                "relation_count": 10,
                "related_to_count": 5,
                "related_to_ratio": 0.5,
                "predicate_confidence_avg": 0.88,
            }
        )
    )

    assert audit["readiness"] == "schema_review"
    assert audit["totals"]["related_to_ratio"] == 0.5


@pytest.mark.asyncio
async def test_ingestion_audit_extraction_unstable_classification():
    audit = await _audit_for(
        _doc(
            {
                "extracted_chunks": 9,
                "failed_chunk_count": 0,
                "ghost_b_success_rate": 0.9,
                "attempt_count": 10,
                "json_recovery_count": 3,
                "relation_count": 10,
                "related_to_ratio": 0.1,
                "predicate_confidence_avg": 0.86,
            }
        )
    )

    assert audit["readiness"] == "extraction_unstable"
    assert audit["totals"]["json_recovery_count"] == 3


@pytest.mark.asyncio
async def test_ingestion_audit_uses_requested_chunks_for_ghost_b_success_rate():
    audit = await _audit_for(
        _doc(
            {
                "requested_chunks": 8,
                "extracted_chunks": 8,
                "failed_chunk_count": 0,
                "ghost_b_success_rate": 1.0,
                "relation_count": 4,
                "related_to_ratio": 0.0,
                "predicate_confidence_avg": 0.9,
            }
        ),
        chunk_count=10,
    )

    assert audit["readiness"] == "ready"
    assert audit["totals"]["ghost_b_requested_chunks"] == 8
    assert audit["totals"]["ghost_b_success_rate"] == 1.0


@pytest.mark.asyncio
async def test_ingestion_audit_reports_extraction_strategy_metrics():
    audit = await _audit_for(
        _doc(
            {
                "requested_chunks": 120,
                "extracted_chunks": 120,
                "failed_chunk_count": 0,
                "ghost_b_success_rate": 1.0,
                "relation_count": 20,
                "related_to_ratio": 0.05,
                "predicate_confidence_avg": 0.9,
                "extraction_strategy": "compact_large_doc",
                "graph_completeness": "graph-compact",
                "skipped_low_value_chunks": 12,
                "compact_extraction_chunks": 120,
                "deep_extraction_chunks": 0,
                "full_extraction_chunks": 0,
                "prompt_tokens": 600000,
                "estimated_cost_tokens": 650000,
                "json_recovery_count": 3,
                "avg_prompt_tokens_per_chunk": 5000.0,
            }
        ),
        chunk_count=132,
    )

    doc = audit["document_metrics"][0]
    assert doc["extraction_strategy"] == "compact_large_doc"
    assert doc["graph_completeness"] == "graph-compact"
    assert doc["skipped_low_value_chunks"] == 12
    assert audit["totals"]["compact_extraction_chunks"] == 120
    assert audit["totals"]["skipped_low_value_chunks"] == 12
    assert audit["totals"]["avg_prompt_tokens_per_chunk"] == 5000.0
    assert audit["totals"]["json_recovery_rate"] == 0.025
