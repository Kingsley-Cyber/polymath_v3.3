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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        self.calls.append((query, params))
        return _Result()


class _Driver:
    def __init__(self):
        self.calls = []

    def session(self):
        return _Session(self.calls)


@pytest.mark.asyncio
async def test_initialize_schema_creates_retrieval_fulltext_indexes():
    driver = _Driver()

    await initialize_schema(driver)

    statements = [query for query, _params in driver.calls]
    assert any("CREATE FULLTEXT INDEX entity_name_ft" in stmt for stmt in statements)
    assert any("CREATE FULLTEXT INDEX fact_text_ft" in stmt for stmt in statements)
    assert any("FOR (e:Entity) ON (e.display_name)" in stmt for stmt in statements)
    assert any("FOR ()-[r:RELATES_TO]-() ON (r.confidence)" in stmt for stmt in statements)
