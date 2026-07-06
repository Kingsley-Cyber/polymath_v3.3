import pytest

from services.ghost_b import EntityItem, ExtractionResult, FactItem, RelationItem
from services.graph.neo4j_writer import (
    delete_corpus_graph,
    delete_document_graph,
    fact_id_from_parts,
    write_document_graph,
)


class FakeSession:
    def __init__(self, calls, tombstone_map=None):
        self.calls = calls
        self.tombstone_map = tombstone_map or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))
        if "tombstone:" in query:
            return FakeResult([
                {"orig": old, "sur": self.tombstone_map[old]}
                for old in params.get("ids", [])
                if old in self.tombstone_map
            ])
        return FakeResult([])


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


class FakeDriver:
    def __init__(self, tombstone_map=None):
        self.calls = []
        self.tombstone_map = tombstone_map or {}

    def session(self):
        return FakeSession(self.calls, self.tombstone_map)


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
    assert "r.support_count" in query
    assert "r.avg_confidence" in query
    assert "r.extract_schema_version" in query
    assert "r.promote_version" in query
    assert "r.last_seen_at" in query
    assert "r.support_confidence_chunk_ids" in query
    assert "r.support_confidence_values" in query
    row = params["rows"][0]
    assert row["chunk_id"] == "c1"
    assert row["doc_id"] == "d1"
    assert row["schema_version"] == "polymath.extract.v1"
    assert params["promote_version"] == "polymath.promote.v1"


@pytest.mark.asyncio
async def test_delete_document_graph_prunes_relation_provenance_before_nodes():
    driver = FakeDriver()

    await delete_document_graph(driver, corpus_id="corp1", doc_id="d1")

    queries = [query for query, _params in driver.calls]
    assert "r.evidence_doc_ids" in queries[0]
    assert "r.support_count" in queries[0]
    assert "r.avg_confidence" in queries[0]
    assert "r.support_confidence_chunk_ids" in queries[0]
    assert "r.support_confidence_values" in queries[0]
    assert "remaining_corpus_support" in queries[0]
    assert "MATCH (n {doc_id: $doc_id, corpus_id: $corpus_id})" in queries[1]
    assert "NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }" in queries[2]
    assert "coalesce(e.tombstone, false) = false" in queries[2]


@pytest.mark.asyncio
async def test_delete_corpus_graph_prunes_array_scoped_relations_before_nodes():
    driver = FakeDriver()

    await delete_corpus_graph(driver, corpus_id="corp1")

    queries = [query for query, _params in driver.calls]
    assert "r.corpus_ids" in queries[0]
    assert "r.evidence_doc_ids" in queries[0]
    assert "r.support_count" in queries[0]
    assert "r.avg_confidence" in queries[0]
    assert "r.support_confidence_chunk_ids" in queries[0]
    assert "r.support_confidence_values" in queries[0]
    assert "WHERE size(coalesce(r.corpus_ids, [])) = 0" in queries[0]
    assert "MATCH (n {corpus_id: $corpus_id})" in queries[1]
    assert "NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }" in queries[2]
    assert "coalesce(e.tombstone, false) = false" in queries[2]


@pytest.mark.asyncio
async def test_write_document_graph_redirects_tombstoned_entities_before_merge():
    driver = FakeDriver(
        tombstone_map={
            "entity:flame_audio": "entity:flameaudio",
        }
    )
    result = ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        entities=[
            EntityItem(
                canonical_name="flame_audio",
                surface_form="flame_audio",
                entity_type="Software",
                confidence=0.9,
            ),
            EntityItem(
                canonical_name="dart",
                surface_form="Dart",
                entity_type="Software",
                confidence=0.8,
            ),
        ],
        relations=[
            RelationItem(
                subject="flame_audio",
                predicate="uses",
                object="dart",
                object_kind="entity",
                confidence=0.7,
            )
        ],
        facts=[
            FactItem(
                subject="flame_audio",
                fact_type="attribute",
                property_name="runtime",
                value="game engine",
                unit=None,
                condition=None,
                confidence=0.8,
                evidence_phrase="flame_audio is a game engine",
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

    entity_call = next(
        (query, params)
        for query, params in driver.calls
        if "MERGE (e:Entity {entity_id: row.entity_id})" in query
    )
    entity_query, entity_params = entity_call
    flame_row = next(
        row for row in entity_params["rows"]
        if row["resolved_from_entity_id"] == "entity:flame_audio"
    )
    assert flame_row["entity_id"] == "entity:flameaudio"
    assert "row.resolved_from_entity_id IS NULL" in entity_query

    relation_params = next(
        params
        for query, params in driver.calls
        if "MERGE (s)-[r:RELATES_TO" in query
    )
    relation_row = relation_params["rows"][0]
    assert relation_row["subject_id"] == "entity:flameaudio"
    assert relation_row["object_id"] == "entity:dart"

    fact_params = next(
        params
        for query, params in driver.calls
        if "MERGE (f:Fact" in query
    )
    fact_row = fact_params["rows"][0]
    assert fact_row["subject_entity_id"] == "entity:flameaudio"


@pytest.mark.asyncio
async def test_write_document_graph_filters_junk_entities_before_merge():
    driver = FakeDriver()
    result = ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        entities=[
            EntityItem(
                canonical_name="And",
                surface_form="And",
                entity_type="Concept",
                confidence=0.9,
            ),
            EntityItem(
                canonical_name="Nash equilibrium",
                surface_form="Nash equilibrium",
                entity_type="Concept",
                confidence=0.95,
            ),
            EntityItem(
                canonical_name="Rule 3",
                surface_form="Rule 3",
                entity_type="Rule",
                confidence=0.8,
            ),
        ],
        relations=[
            RelationItem(
                subject="And",
                predicate="related_to",
                object="Nash equilibrium",
                object_kind="entity",
                confidence=0.7,
            ),
            RelationItem(
                subject="Nash equilibrium",
                predicate="uses",
                object="Rule 3",
                object_kind="entity",
                confidence=0.7,
            ),
        ],
        facts=[
            FactItem(
                subject="And",
                fact_type="attribute",
                property_name="noise",
                value="true",
                unit=None,
                condition=None,
                confidence=0.9,
                evidence_phrase=None,
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

    entity_params = next(
        params
        for query, params in driver.calls
        if "MERGE (e:Entity {entity_id: row.entity_id})" in query
    )
    assert [row["entity_id"] for row in entity_params["rows"]] == [
        "entity:nash-equilibrium"
    ]
    assert [row["display_name"] for row in entity_params["rows"]] == [
        "Nash equilibrium"
    ]
    assert not [
        params
        for query, params in driver.calls
        if "MERGE (s)-[r:RELATES_TO" in query
    ]
    assert not [
        params
        for query, params in driver.calls
        if "MERGE (f:Fact" in query
    ]
