import pytest
from neo4j.exceptions import TransientError

from services.graph import neo4j_writer


def _transient(code: str) -> TransientError:
    exc = TransientError(code)
    exc.code = code
    return exc


@pytest.mark.asyncio
async def test_write_document_graph_retries_deadlock(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    async def fake_write_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _transient("Neo.TransientError.Transaction.DeadlockDetected")

    async def fake_sleep(delay: float):
        sleeps.append(delay)

    monkeypatch.setattr(neo4j_writer, "_write_document_graph_once", fake_write_once)
    monkeypatch.setattr(neo4j_writer.asyncio, "sleep", fake_sleep)

    await neo4j_writer.write_document_graph(
        driver=object(),
        doc_id="doc-1",
        corpus_id="corpus-1",
        extraction_results=[],
    )

    assert calls == 2
    assert sleeps == [neo4j_writer.GRAPH_WRITE_DEADLOCK_BACKOFF_SECONDS]


@pytest.mark.asyncio
async def test_write_document_graph_does_not_retry_non_deadlock_transient(monkeypatch):
    calls = 0

    async def fake_write_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise _transient("Neo.TransientError.General.DatabaseUnavailable")

    monkeypatch.setattr(neo4j_writer, "_write_document_graph_once", fake_write_once)

    with pytest.raises(TransientError):
        await neo4j_writer.write_document_graph(
            driver=object(),
            doc_id="doc-1",
            corpus_id="corpus-1",
            extraction_results=[],
        )

    assert calls == 1
