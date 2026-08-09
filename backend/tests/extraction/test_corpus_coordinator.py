"""Station B: corpus overlap changes WHEN work happens, never WHAT exists."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.extraction.corpus_coordinator import (
    CorpusDocument,
    run_corpus_factory,
)
from tests.extraction.test_graphify_pipeline import _Db, _NamedProvider


def _documents():
    texts = {
        "doc-a": "Graphify uses MongoDB for canonical storage.",
        "doc-b": "Harbor depends on SQLite during recovery.",
        "doc-c": "Beacon Queue supports the Ledger Store.",
    }
    specs = (
        ("Graphify", "software"), ("MongoDB", "software"),
        ("Harbor", "software"), ("SQLite", "software"),
        ("Beacon Queue", "software"), ("Ledger Store", "software"),
    )
    documents = [
        CorpusDocument(doc_id, text, [SimpleNamespace(chunk_id=f"{doc_id}:1", text=text)])
        for doc_id, text in texts.items()
    ]
    return documents, specs


@pytest.mark.asyncio
async def test_concurrent_corpus_digest_equals_serial() -> None:
    documents, specs = _documents()
    serial = await run_corpus_factory(
        db=_Db(), corpus_id="corpus", documents=documents,
        provider=_NamedProvider(specs), max_active=1,
    )
    concurrent = await run_corpus_factory(
        db=_Db(), corpus_id="corpus", documents=documents,
        provider=_NamedProvider(specs), max_active=3,
    )
    assert serial.corpus_digest == concurrent.corpus_digest
    assert [row["status"] for row in concurrent.per_document] == ["passed"] * 3
    assert concurrent.report["max_active_documents"] == 3
    assert concurrent.report["overlap_factor"] >= 1.0


@pytest.mark.asyncio
async def test_one_failed_document_never_sinks_the_corpus() -> None:
    documents, specs = _documents()

    class ExplodingDb(_Db):
        def __missing__(self, key):
            raise RuntimeError("db down")

    result = await run_corpus_factory(
        db=ExplodingDb(), corpus_id="corpus", documents=documents[:1],
        provider=_NamedProvider(specs), max_active=1,
    )
    assert result.per_document[0]["status"] == "failed"
    assert result.report["failed"] == 1
