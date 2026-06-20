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


@pytest.mark.asyncio
async def test_fact_retrieval_balances_indexed_entities_and_prefers_semantic_facts():
    captured: dict = {}
    rows = [
        {
            "fact_id": "fact:python",
            "subject": "Python",
            "fact_type": "property",
            "property_name": "language",
            "value": "programming language",
            "unit": None,
            "condition": None,
            "confidence": 0.91,
            "evidence_phrase": "Python is a programming language",
            "chunk_id": "chunk-python",
            "doc_id": "doc-python",
            "corpus_id": "corpus-1",
        },
        {
            "fact_id": "fact:ai",
            "subject": "artificial intelligence",
            "fact_type": "property",
            "property_name": "definition",
            "value": "systems capable of tasks requiring intelligence",
            "unit": None,
            "condition": None,
            "confidence": 0.9,
            "evidence_phrase": "tasks that typically require human intelligence",
            "chunk_id": "chunk-ai",
            "doc_id": "doc-ai",
            "corpus_id": "corpus-1",
        },
    ]
    svc = FactRetrieval.__new__(FactRetrieval)
    svc._settings = SimpleNamespace(NEO4J_ENABLED=True)
    svc._driver = _FakeDriver(rows, captured)

    facts = await svc.retrieve_facts_for_entities(
        [],
        corpus_ids=["corpus-1"],
        fact_types=None,
        limit=8,
        entity_ids=["entity:python", "entity:artificial-intelligence"],
    )

    cypher = captured["cypher"]
    assert "UNWIND $entity_ids AS entity_id" in cypher
    assert "[0..$per_entity_limit]" in cypher
    assert "semantic_rank DESC" in cypher
    assert "timestamp', 'threshold'" in cypher
    assert captured["params"]["per_entity_limit"] == 4
    assert len(facts) == 2
    assert {fact.subject for fact in facts} == {"Python", "artificial intelligence"}
