import pytest

from services.ghost_b import EntityItem, ExtractionResult, FactItem, RelationItem
from services.graph.neo4j_writer import (
    delete_corpus_graph,
    delete_document_graph,
    fact_id_from_parts,
    write_document_graph,
)


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))


class FakeDriver:
    def __init__(self):
        self.calls = []

    def session(self):
        return FakeSession(self.calls)


@pytest.mark.asyncio
async def test_write_document_graph_persists_structured_facts():
    driver = FakeDriver()
    result = ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        entities=[
            EntityItem(
                canonical_name="cache timeout",
                surface_form="cache timeout",
                entity_type="Concept",
                confidence=0.9,
            )
        ],
        relations=[],
        facts=[
            FactItem(
                subject="cache timeout",
                fact_type="threshold",
                property_name="duration",
                value="30",
                unit="seconds",
                condition=None,
                confidence=0.95,
                evidence_phrase="The cache timeout is 30 seconds",
            )
        ],
    )

    await write_document_graph(
        driver=driver,
        doc_id="d1",
        corpus_id="corp1",
        extraction_results=[result],
        user_id="u1",
        file_id="f1",
        all_chunk_ids=["c1"],
    )

    fact_calls = [
        (query, params)
        for query, params in driver.calls
        if "MERGE (f:Fact" in query
    ]
    assert len(fact_calls) == 1
    query, params = fact_calls[0]
    assert "HAS_FACT" in query
    assert "SUPPORTS_FACT" in query
    row = params["rows"][0]
    assert row["fact_id"] == fact_id_from_parts(
        doc_id="d1",
        chunk_id="c1",
        subject="cache timeout",
        property_name="duration",
        value="30",
    )
    assert row["subject_entity_id"] == "entity:cache-timeout"
    assert row["fact_type"] == "threshold"
    assert row["property_name"] == "duration"
    assert row["value"] == "30"
    assert row["unit"] == "seconds"
    assert row["evidence_phrase"] == "The cache timeout is 30 seconds"


@pytest.mark.asyncio
async def test_write_document_graph_persists_relation_doc_provenance():
    driver = FakeDriver()
    result = ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        entities=[
            EntityItem(
                canonical_name="lambda",
                surface_form="Lambda",
                entity_type="Software",
                confidence=0.9,
            ),
            EntityItem(
                canonical_name="s3",
                surface_form="S3",
                entity_type="Software",
                confidence=0.9,
            ),
        ],
        relations=[
            RelationItem(
                subject="lambda",
                predicate="uses",
                object="s3",
                object_kind="entity",
                confidence=0.88,
                evidence_phrase="Lambda uses S3 events",
            )
        ],
    )

    await write_document_graph(
        driver=driver,
        doc_id="d1",
        corpus_id="corp1",
        extraction_results=[result],
        user_id="u1",
        file_id="f1",
        all_chunk_ids=["c1"],
    )

    relation_calls = [
        (query, params)
        for query, params in driver.calls
        if "MERGE (s)-[r:RELATES_TO" in query
    ]
    assert len(relation_calls) == 1
    query, params = relation_calls[0]
    assert "r.evidence_chunk_ids" in query
    assert "r.evidence_doc_ids" in query
    assert "r.latest_doc_id" in query
    row = params["rows"][0]
    assert row["chunk_id"] == "c1"
    assert row["doc_id"] == "d1"


@pytest.mark.asyncio
async def test_delete_document_graph_prunes_relation_provenance_before_nodes():
    driver = FakeDriver()

    await delete_document_graph(driver, corpus_id="corp1", doc_id="d1")

    queries = [query for query, _params in driver.calls]
    assert "r.evidence_doc_ids" in queries[0]
    assert "remaining_corpus_support" in queries[0]
    assert "MATCH (n {doc_id: $doc_id, corpus_id: $corpus_id})" in queries[1]
    assert "NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }" in queries[2]


@pytest.mark.asyncio
async def test_delete_corpus_graph_prunes_array_scoped_relations_before_nodes():
    driver = FakeDriver()

    await delete_corpus_graph(driver, corpus_id="corp1")

    queries = [query for query, _params in driver.calls]
    assert "r.corpus_ids" in queries[0]
    assert "r.evidence_doc_ids" in queries[0]
    assert "WHERE size(coalesce(r.corpus_ids, [])) = 0" in queries[0]
    assert "MATCH (n {corpus_id: $corpus_id})" in queries[1]
    assert "NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }" in queries[2]
