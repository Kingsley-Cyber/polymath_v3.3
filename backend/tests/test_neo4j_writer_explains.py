"""Neo4j writer — mixed-content book lane (Slice 1).

EXPLAINS edges are deterministic document-structural edges between Chunk
nodes, written from tier_chunker's adjacency rows. Chunk nodes also carry
chunk_kind / language so graph queries can filter the same way Qdrant
payloads do. Mock-session tests: no live Neo4j required.
"""

import pytest

from services.graph.neo4j_writer import write_document_graph


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def single(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))
        return FakeResult([])


class FakeDriver:
    def __init__(self):
        self.calls = []

    def session(self):
        return FakeSession(self.calls)


@pytest.mark.asyncio
async def test_write_document_graph_emits_explains_edges():
    driver = FakeDriver()
    await write_document_graph(
        driver=driver,
        doc_id="d1",
        corpus_id="corp1",
        extraction_results=[],
        all_chunk_ids=["c_prose", "c_code"],
        chunk_attributes={
            "c_prose": {"chunk_kind": "body", "language": None},
            "c_code": {"chunk_kind": "code", "language": "python"},
        },
        explains_rows=[
            {
                "relation": "EXPLAINS",
                "basis": "adjacency",
                "explains_chunk_id": "c_prose",
                "code_chunk_id": "c_code",
            }
        ],
    )

    # EXPLAINS MERGE fired once, corpus/doc-scoped, with the mapped ids.
    explains_calls = [
        (query, params)
        for query, params in driver.calls
        if "MERGE (prose)-[e:EXPLAINS]->(code)" in query
    ]
    assert len(explains_calls) == 1
    query, params = explains_calls[0]
    assert params["corpus_id"] == "corp1"
    assert params["doc_id"] == "d1"
    assert "corpus_id: $corpus_id" in query
    assert params["rows"] == [
        {
            "prose_chunk_id": "c_prose",
            "code_chunk_id": "c_code",
            "basis": "adjacency",
        }
    ]

    # Chunk MERGE batch carries chunk_kind / language per chunk.
    chunk_calls = [
        (query, params)
        for query, params in driver.calls
        if "MERGE (c:Chunk" in query and "UNWIND" in query
    ]
    assert chunk_calls
    _query, chunk_params = chunk_calls[0]
    rows_by_id = {row["chunk_id"]: row for row in chunk_params["rows"]}
    assert rows_by_id["c_prose"]["chunk_kind"] == "body"
    assert rows_by_id["c_prose"]["language"] is None
    assert rows_by_id["c_code"]["chunk_kind"] == "code"
    assert rows_by_id["c_code"]["language"] == "python"


@pytest.mark.asyncio
async def test_write_document_graph_explains_empty_rows_noop():
    driver = FakeDriver()
    await write_document_graph(
        driver=driver,
        doc_id="d1",
        corpus_id="corp1",
        extraction_results=[],
        all_chunk_ids=["c_prose"],
        explains_rows=[],
    )
    assert not any("EXPLAINS" in query for query, _params in driver.calls)


@pytest.mark.asyncio
async def test_write_document_graph_explains_skips_malformed_rows():
    driver = FakeDriver()
    await write_document_graph(
        driver=driver,
        doc_id="d1",
        corpus_id="corp1",
        extraction_results=[],
        all_chunk_ids=["c_prose", "c_code"],
        explains_rows=[
            {"explains_chunk_id": "", "code_chunk_id": "c_code"},
            {"explains_chunk_id": "c_prose", "code_chunk_id": None},
        ],
    )
    # Both rows are malformed (empty endpoint id) — no EXPLAINS MERGE fires.
    assert not any(
        "MERGE (prose)-[e:EXPLAINS]->(code)" in query
        for query, _params in driver.calls
    )
