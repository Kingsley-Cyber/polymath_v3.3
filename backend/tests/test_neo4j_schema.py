import pytest

from services.graph.schema import initialize_schema


class _Result:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _Session:
    def __init__(self, calls):
        self.calls = calls
        self.constraints = [
            {
                "name": "legacy_document_doc_id",
                "entityType": "NODE",
                "labelsOrTypes": ["Document"],
                "properties": ["doc_id"],
            },
            {
                "name": "legacy_chunk_chunk_id",
                "entityType": "NODE",
                "labelsOrTypes": ["Chunk"],
                "properties": ["chunk_id"],
            },
            {
                "name": "legacy_fact_fact_id",
                "entityType": "NODE",
                "labelsOrTypes": ["Fact"],
                "properties": ["fact_id"],
            },
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))
        if "CREATE CONSTRAINT document_corpus_doc_id_unique" in query:
            self.constraints.append(
                {
                    "name": "document_corpus_doc_id_unique",
                    "entityType": "NODE",
                    "labelsOrTypes": ["Document"],
                    "properties": ["corpus_id", "doc_id"],
                }
            )
        elif "CREATE CONSTRAINT chunk_corpus_chunk_id_unique" in query:
            self.constraints.append(
                {
                    "name": "chunk_corpus_chunk_id_unique",
                    "entityType": "NODE",
                    "labelsOrTypes": ["Chunk"],
                    "properties": ["corpus_id", "chunk_id"],
                }
            )
        elif "CREATE CONSTRAINT fact_corpus_fact_id_unique" in query:
            self.constraints.append(
                {
                    "name": "fact_corpus_fact_id_unique",
                    "entityType": "NODE",
                    "labelsOrTypes": ["Fact"],
                    "properties": ["corpus_id", "fact_id"],
                }
            )
        elif query.startswith("DROP CONSTRAINT"):
            dropped = query.split("`", 2)[1]
            self.constraints = [
                row for row in self.constraints if row["name"] != dropped
            ]
        elif query.startswith("SHOW CONSTRAINTS"):
            return _Result(self.constraints)
        return _Result()


class _Driver:
    def __init__(self):
        self.calls = []
        self._session = _Session(self.calls)

    def session(self):
        return self._session


@pytest.mark.asyncio
async def test_initialize_schema_creates_retrieval_fulltext_indexes():
    driver = _Driver()

    await initialize_schema(driver)

    statements = [query for query, _params in driver.calls]
    assert any("CREATE FULLTEXT INDEX entity_name_ft" in stmt for stmt in statements)
    assert any("CREATE FULLTEXT INDEX fact_text_ft" in stmt for stmt in statements)
    assert any("FOR (e:Entity) ON (e.display_name)" in stmt for stmt in statements)
    assert any(
        "FOR ()-[r:RELATES_TO]-() ON (r.confidence)" in stmt for stmt in statements
    )
    assert sum(stmt.startswith("DROP CONSTRAINT") for stmt in statements) == 3
