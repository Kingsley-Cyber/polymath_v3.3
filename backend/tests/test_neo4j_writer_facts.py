import pytest

from services.ghost_b import EntityItem, ExtractionResult, FactItem
from services.graph.neo4j_writer import fact_id_from_parts, write_document_graph


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
