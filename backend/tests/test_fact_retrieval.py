from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.retriever.fact_retrieval import FactRetrieval


class _FakeResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeSession:
    def __init__(self, rows: list[dict], captured: dict):
        self._rows = rows
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def run(self, cypher: str, **params):
        self._captured["cypher"] = cypher
        self._captured["params"] = params
        return _FakeResult(self._rows)


class _FakeDriver:
    def __init__(self, rows: list[dict], captured: dict):
        self._rows = rows
        self._captured = captured

    def session(self):
        return _FakeSession(self._rows, self._captured)


@pytest.mark.asyncio
async def test_fact_retrieval_filters_entities_before_optional_support_match():
    captured: dict = {}
    rows = [
        {
            "fact_id": "fact:1",
            "subject": "Domain Model",
            "fact_type": "property",
            "property_name": "describes",
            "value": "domain logic",
            "unit": None,
            "condition": None,
            "confidence": 0.91,
            "evidence_phrase": "domain logic",
            "chunk_id": "chunk-1",
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
        }
    ]
    svc = FactRetrieval.__new__(FactRetrieval)
    svc._settings = SimpleNamespace(NEO4J_ENABLED=True)
    svc._driver = _FakeDriver(rows, captured)

    facts = await svc.retrieve_facts_for_entities(
        ["Domain Model"],
        corpus_ids=["corpus-1"],
        fact_types=None,
    )

    cypher = captured["cypher"]
    assert cypher.index("WHERE (") < cypher.index("OPTIONAL MATCH")
    assert captured["params"]["entity_names_lc"] == ["domain model"]
    assert len(facts) == 1
    assert facts[0].chunk_id == "chunk-1"

