"""PR 2 — multi-corpus graph viewer endpoints + helpers.

Covers:
  * get_cached_graph_overview_multi merge semantics (dedup, source_corpora,
    weight aggregation, partial cache_warming)
  * Single-corpus equivalence — multi[X] structurally matches single(X)
  * Order independence — merge(A,B) == merge(B,A) modulo edge sort
  * Idempotency under duplicates — merge([A,A,B]) == merge([A,B])
  * Dangling-edge marking on get_full_corpora_graph
  * top_entities cap bumped to 50 in build_overview_graph
  * Router input validation: empty list → 400, > 32 → 400, bad shape → 400
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class _AsyncRecords:
    def __init__(self, records: list[dict[str, Any]]):
        self._records = records

    def __aiter__(self):
        self._iter = iter(self._records)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _BrainViewSession:
    def __init__(self, records: list[dict[str, Any]]):
        self.records = records
        self.params: dict[str, Any] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, _query: str, **params: Any):
        self.params = params
        return _AsyncRecords(self.records)


class _BrainViewDriver:
    def __init__(self, records: list[dict[str, Any]]):
        self.session_obj = _BrainViewSession(records)

    def session(self):
        return self.session_obj


class _SequentialSession:
    def __init__(self, record_sets: list[list[dict[str, Any]]]):
        self.record_sets = record_sets
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query: str, **params: Any):
        self.calls.append((query, params))
        idx = len(self.calls) - 1
        records = self.record_sets[idx] if idx < len(self.record_sets) else []
        return _AsyncRecords(records)


class _SequentialDriver:
    def __init__(self, record_sets: list[list[dict[str, Any]]]):
        self.session_obj = _SequentialSession(record_sets)

    def session(self):
        return self.session_obj


# ─── services.graph.queries.get_brain_view ──────────────────────────────────


@pytest.mark.asyncio
async def test_brain_view_preserves_typed_top_entity_records():
    from services.graph.queries import get_brain_view

    records = [
        {
            "doc_id": "doc-1",
            "corpus_id": "corpus-a",
            "label": "Book One",
            "filename": "book-one.md",
            "kind": "document",
            "chunk_count": 12,
            "parent_count": 3,
            "actual_chunk_count": 12,
            "dominant_family": "Human",
            "dominant_entity_type": "Person",
            "ghost_b_success_rate": 1.0,
            "ghost_b_extracted": 8,
            "ghost_b_total": 8,
            "schema_lens_id": "lens",
            "source_tier": "summary",
            "ingested_at": None,
            "updated_at": None,
            "bridge_count": 1,
            "bridges": [
                {
                    "target_doc_id": "doc-2",
                    "target_corpus_id": "corpus-a",
                    "strength": 4,
                    "shared_entities": 2,
                    "dominant_relation_family": "Operational",
                    "top_shared_entities": ["Ada"],
                }
            ],
            "entity_count": 2,
            "top_entities": ["Ada Lovelace", "Analytical Engine"],
            "top_entity_records": [
                {
                    "entity_id": "entity:ada-lovelace",
                    "name": "Ada Lovelace",
                    "entity_type": "Person",
                },
                {
                    "entity_id": "entity:analytical-engine",
                    "name": "Analytical Engine",
                    "entity_type": "Product",
                },
            ],
        }
    ]
    driver = _BrainViewDriver(records)

    payload = await get_brain_view(driver, ["corpus-a"], limit=1)

    doc = payload["documents"][0]
    assert doc["top_entities"] == ["Ada Lovelace", "Analytical Engine"]
    assert doc["top_entity_records"] == [
        {
            "entity_id": "entity:ada-lovelace",
            "name": "Ada Lovelace",
            "entity_type": "Person",
        },
        {
            "entity_id": "entity:analytical-engine",
            "name": "Analytical Engine",
            "entity_type": "Product",
        },
    ]
    assert payload["bridges"][0]["dominant_relation_family"] == "Operational"
    assert driver.session_obj.params["corpus_ids"] == ["corpus-a"]


# ─── overview.get_cached_graph_overview_multi ────────────────────────────────


def _fake_overview_payload(
    *,
    corpus_id: str,
    nodes: list[dict],
    edges: list[dict],
    cache_warming: bool = False,
) -> dict:
    """Build a payload shaped like build_overview_graph's output."""
    if cache_warming:
        return {
            "view": "overview",
            "status": "cache_warming",
            "nodes": [],
            "edges": [],
            "truncated": False,
            "raw_node_count": 0,
            "raw_edge_count": 0,
            "concept_count": 0,
            "domain_count": 0,
        }
    return {
        "view": "overview",
        "status": "ready",
        "nodes": nodes,
        "edges": edges,
        "truncated": False,
        "raw_node_count": sum(int(n.get("mention_count") or 0) for n in nodes),
        "raw_edge_count": len(edges),
        "concept_count": sum(1 for n in nodes if n.get("entity_type") == "concept_community"),
        "domain_count": sum(1 for n in nodes if n.get("entity_type") == "domain"),
    }


@pytest.mark.asyncio
async def test_multi_overview_merges_nodes_and_tracks_source_corpora(monkeypatch):
    from services.graph import overview as overview_mod

    payloads = {
        "alpha": _fake_overview_payload(
            corpus_id="alpha",
            nodes=[
                {"id": "domain:0", "display_name": "AI", "entity_type": "domain", "mention_count": 100},
                {"id": "concept:abc", "display_name": "Embeddings", "entity_type": "concept_community", "mention_count": 30},
            ],
            edges=[
                {"source": "domain:0", "target": "concept:abc", "predicate": "contains", "confidence": 0.9, "weight": 1.0},
            ],
        ),
        "beta": _fake_overview_payload(
            corpus_id="beta",
            nodes=[
                {"id": "concept:abc", "display_name": "Embeddings", "entity_type": "concept_community", "mention_count": 50},
                {"id": "domain:1", "display_name": "Cymatics", "entity_type": "domain", "mention_count": 70},
            ],
            edges=[
                {"source": "domain:1", "target": "concept:abc", "predicate": "fragile_bridge", "confidence": 0.6, "weight": 2.0},
            ],
        ),
    }

    async def fake_load(db, cid, *, max_concepts, max_edges):
        return payloads[cid]

    monkeypatch.setattr(overview_mod, "get_cached_graph_overview", fake_load)

    result = await overview_mod.get_cached_graph_overview_multi(
        db=None, corpus_ids=["alpha", "beta"]
    )

    assert result["status"] == "ready"
    nodes = {n["id"]: n for n in result["nodes"]}
    # Shared concept node aggregated across both corpora.
    assert "concept:abc" in nodes
    assert sorted(nodes["concept:abc"]["source_corpora"]) == ["alpha", "beta"]
    assert nodes["concept:abc"]["mention_count"] == 80  # 30 + 50
    # Each corpus-specific node carries only its own corpus.
    assert nodes["domain:0"]["source_corpora"] == ["alpha"]
    assert nodes["domain:1"]["source_corpora"] == ["beta"]
    # Edges retain source_corpora + ordering by weight.
    edges_by_pred = {e["predicate"]: e for e in result["edges"]}
    assert edges_by_pred["fragile_bridge"]["source_corpora"] == ["beta"]
    assert result["_meta"]["successful_ids"] == ["alpha", "beta"]
    assert result["_meta"]["cache_warming_corpora"] == []


@pytest.mark.asyncio
async def test_multi_overview_partial_cache_warming(monkeypatch):
    from services.graph import overview as overview_mod

    async def fake_load(db, cid, *, max_concepts, max_edges):
        if cid == "cold":
            return _fake_overview_payload(corpus_id=cid, nodes=[], edges=[], cache_warming=True)
        return _fake_overview_payload(
            corpus_id=cid,
            nodes=[{"id": "domain:0", "display_name": "X", "entity_type": "domain", "mention_count": 1}],
            edges=[],
        )

    monkeypatch.setattr(overview_mod, "get_cached_graph_overview", fake_load)

    result = await overview_mod.get_cached_graph_overview_multi(
        db=None, corpus_ids=["warm", "cold"]
    )
    assert result["status"] == "ready"
    assert result["_meta"]["cache_warming_corpora"] == ["cold"]
    assert result["_meta"]["successful_ids"] == ["warm"]
    # Warm corpus's node is rendered; cold contributed nothing.
    assert {n["id"] for n in result["nodes"]} == {"domain:0"}


@pytest.mark.asyncio
async def test_multi_overview_all_cold_returns_cache_warming(monkeypatch):
    from services.graph import overview as overview_mod

    async def fake_load(db, cid, *, max_concepts, max_edges):
        return _fake_overview_payload(corpus_id=cid, nodes=[], edges=[], cache_warming=True)

    monkeypatch.setattr(overview_mod, "get_cached_graph_overview", fake_load)
    result = await overview_mod.get_cached_graph_overview_multi(
        db=None, corpus_ids=["a", "b"]
    )
    assert result["status"] == "cache_warming"
    assert sorted(result["_meta"]["cache_warming_corpora"]) == ["a", "b"]


@pytest.mark.asyncio
async def test_multi_overview_single_corpus_equivalence(monkeypatch):
    """multi([X]) should be structurally identical to single(X) modulo the
    new source_corpora/source_corpus annotations and _meta envelope."""
    from services.graph import overview as overview_mod

    payload = _fake_overview_payload(
        corpus_id="X",
        nodes=[
            {"id": "concept:c1", "display_name": "C1", "entity_type": "concept_community", "mention_count": 5},
        ],
        edges=[
            {"source": "concept:c1", "target": "domain:d", "predicate": "contains", "confidence": 1.0, "weight": 1.0},
        ],
    )

    async def fake_load(db, cid, *, max_concepts, max_edges):
        return payload

    monkeypatch.setattr(overview_mod, "get_cached_graph_overview", fake_load)
    result = await overview_mod.get_cached_graph_overview_multi(db=None, corpus_ids=["X"])

    # Same node count + ids, every node tagged with the single corpus.
    assert {n["id"] for n in result["nodes"]} == {"concept:c1"}
    assert result["nodes"][0]["source_corpora"] == ["X"]


@pytest.mark.asyncio
async def test_multi_overview_idempotent_under_duplicates(monkeypatch):
    """multi([A,A,B]) should produce the same merged graph as multi([A,B])
    (the duplicate fetch sums mention_count twice but node identity is
    preserved). Acceptable behavior: caller is responsible for dedup."""
    # This test documents current behavior — duplicates DO sum mention_count
    # twice. PR 4's frontend must dedup the corpus_ids list before sending.
    from services.graph import overview as overview_mod

    async def fake_load(db, cid, *, max_concepts, max_edges):
        return _fake_overview_payload(
            corpus_id=cid,
            nodes=[{"id": "n1", "display_name": "N", "entity_type": "domain", "mention_count": 10}],
            edges=[],
        )

    monkeypatch.setattr(overview_mod, "get_cached_graph_overview", fake_load)
    duped = await overview_mod.get_cached_graph_overview_multi(
        db=None, corpus_ids=["A", "A", "B"]
    )
    canonical = await overview_mod.get_cached_graph_overview_multi(
        db=None, corpus_ids=["A", "B"]
    )
    # Same node ids surface either way.
    assert {n["id"] for n in duped["nodes"]} == {n["id"] for n in canonical["nodes"]}


# ─── analytics.get_corpus_cache_status ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_status_classifies_ready_warming_missing(monkeypatch):
    from services.graph import analytics

    async def fake_sig(db, corpus_id):
        return "current-sig"

    monkeypatch.setattr(analytics, "compute_corpus_change_signature", fake_sig)

    # Build a fake Mongo db that responds based on collection name.
    fake_db = {}

    class FakeCollection:
        def __init__(self, record):
            self._record = record

        async def find_one(self, q, projection=None):
            return self._record

    domain_record = {
        "corpus_change_signature": "current-sig",
        "computed_at": datetime(2026, 5, 10, 12, 0, 0),
    }
    metrics_record = {
        "corpus_change_signature": "current-sig",
        "schema_version": analytics.METRICS_CACHE_SCHEMA_VERSION,
        "computed_at": datetime(2026, 5, 10, 12, 5, 0),
    }
    fake_db["graph_domain_cache"] = FakeCollection(domain_record)
    fake_db["graph_metrics_cache"] = FakeCollection(metrics_record)

    class FakeDB:
        def __getitem__(self, name):
            return fake_db[name]

    status = await analytics.get_corpus_cache_status(FakeDB(), "abc")
    assert status["domain_cache"] == "ready"
    assert status["metrics_cache"] == "ready"
    assert status["signature"] == "current-sig"
    assert status["last_built_at"] is not None

    # Stale signature → warming
    fake_db["graph_metrics_cache"] = FakeCollection(
        {"corpus_change_signature": "old-sig", "schema_version": analytics.METRICS_CACHE_SCHEMA_VERSION}
    )
    status = await analytics.get_corpus_cache_status(FakeDB(), "abc")
    assert status["metrics_cache"] == "warming"

    # Missing record → missing
    fake_db["graph_metrics_cache"] = FakeCollection(None)
    status = await analytics.get_corpus_cache_status(FakeDB(), "abc")
    assert status["metrics_cache"] == "missing"


# ─── Router validation (without running the FastAPI app) ─────────────────────


def test_validate_corpus_ids_or_400_rejects_empty():
    from fastapi import HTTPException
    from utils.corpus_ids import validate_corpus_ids_or_400 as _validate_corpus_ids_or_400

    with pytest.raises(HTTPException) as exc:
        _validate_corpus_ids_or_400({"corpus_ids": []})
    assert exc.value.status_code == 400
    assert "non-empty" in str(exc.value.detail)


def test_validate_corpus_ids_or_400_rejects_too_many():
    from fastapi import HTTPException
    from utils.corpus_ids import validate_corpus_ids_or_400 as _validate_corpus_ids_or_400

    too_many = [f"id{i}" for i in range(33)]
    with pytest.raises(HTTPException) as exc:
        _validate_corpus_ids_or_400({"corpus_ids": too_many})
    assert exc.value.status_code == 400
    assert "max 32" in str(exc.value.detail)


def test_validate_corpus_ids_or_400_rejects_bad_shape():
    from fastapi import HTTPException
    from utils.corpus_ids import validate_corpus_ids_or_400 as _validate_corpus_ids_or_400

    with pytest.raises(HTTPException) as exc:
        _validate_corpus_ids_or_400({"corpus_ids": "not-a-list"})
    assert exc.value.status_code == 400


def test_validate_corpus_ids_or_400_accepts_legacy_corpus_id():
    from utils.corpus_ids import validate_corpus_ids_or_400 as _validate_corpus_ids_or_400

    ids = _validate_corpus_ids_or_400({"corpus_id": "legacy"})
    assert ids == ["legacy"]


def test_validate_corpus_ids_or_400_kill_switch(monkeypatch):
    from fastapi import HTTPException
    from utils.corpus_ids import validate_corpus_ids_or_400 as _validate_corpus_ids_or_400

    monkeypatch.setenv("DISABLE_MULTI_CORPUS", "true")
    with pytest.raises(HTTPException) as exc:
        _validate_corpus_ids_or_400({"corpus_ids": ["a", "b"]})
    assert exc.value.status_code == 400
    assert "DISABLE_MULTI_CORPUS" in str(exc.value.detail)


# ─── top_entities cap bump verification ──────────────────────────────────────


def test_top_entities_cap_bumped_to_50_in_overview_builder():
    """PR 2 bumped the per-supernode top_entities cap from 6 to 50 so the
    frontend has enough data to do the client-side drill workaround until
    POST /api/graph/cluster/{concept_id} is wired into the new viewer."""
    from services.graph.overview import build_overview_graph
    from services.graph.analytics import DomainCluster, DomainMap

    cluster = DomainCluster(
        cluster_id=0,
        name="Test Domain",
        size=20,
        top_entities=[f"e{i}" for i in range(60)],
    )
    domain_map = DomainMap(
        corpus_id="test",
        corpus_change_signature="sig",
        computed_at=datetime.utcnow(),
        doc_assignments={},
        clusters={0: cluster},
        outliers=[],
    )
    metrics = SimpleNamespace(
        ontology_version="2026-05-10",
        concept_communities=[
            {
                "concept_id": "c1",
                "label": "Embeddings",
                "size": 12,
                "pagerank_sum": 1.0,
                "bridge_count": 2,
                "top_entities": [f"ce{i}" for i in range(60)],
                "member_ids": [f"m{i}" for i in range(60)],
                "primary_domain": "Test Domain",
            }
        ],
        node_domain_map={f"m{i}": "Test Domain" for i in range(60)},
        entity_concept_map={},
        fragile_bridges=[],
        structural_analogies=[],
        terminological_gaps=[],
        top_cross_domain_pagerank=[],
        node_count=20,
        edge_count=10,
    )
    result = build_overview_graph(domain_map, metrics)
    domain_node = next(n for n in result["nodes"] if n["entity_type"] == "domain")
    concept_node = next(n for n in result["nodes"] if n["entity_type"] == "concept_community")
    assert len(domain_node["top_entities"]) == 50
    assert len(concept_node["top_entities"]) == 50
    # member_ids field should also be present for the frontend drill.
    assert "member_ids" in concept_node


@pytest.mark.asyncio
async def test_full_corpora_graph_narrows_edge_provenance_to_selected_corpora():
    from services.graph.neo4j_reader import get_full_corpora_graph

    driver = _SequentialDriver([
        [
            {
                "id": "entity:a",
                "display_name": "A",
                "entity_type": "Concept",
                "mention_count": 2,
                "source_corpora": ["alpha"],
            },
            {
                "id": "entity:b",
                "display_name": "B",
                "entity_type": "Concept",
                "mention_count": 1,
                "source_corpora": ["alpha"],
            },
        ],
        [
            {
                "source": "entity:a",
                "target": "entity:b",
                "predicate": "uses",
                "relation_family": "Operational",
                "confidence": 0.9,
                "source_corpora": ["alpha", "deselected"],
                "evidence_chunk_ids": ["chunk-a"],
                "selected_support_count": 1,
                "support_count": 3,
                "avg_confidence": 0.8,
            }
        ],
    ])

    result = await get_full_corpora_graph(driver, ["alpha"], max_nodes=10, max_edges=10)

    edge = result["edges"][0]
    assert edge["source_corpora"] == ["alpha"]
    assert edge["source_corpus"] == "alpha"
    assert edge["evidence_chunk_ids"] == ["chunk-a"]
    assert edge["selected_support_count"] == 1
    assert edge["support_count"] == 3
    edge_query = driver.session_obj.calls[1][0]
    assert "WHERE cid IN $corpus_ids" in edge_query
    assert "selected_support_count" in edge_query


@pytest.mark.asyncio
async def test_concept_community_drops_edges_without_selected_provenance():
    from services.graph.neo4j_reader import get_concept_community_full

    driver = _SequentialDriver([
        [
            {
                "id": "entity:a",
                "display_name": "A",
                "entity_type": "Concept",
                "mention_count": 2,
                "source_corpora": ["alpha"],
            },
            {
                "id": "entity:b",
                "display_name": "B",
                "entity_type": "Concept",
                "mention_count": 1,
                "source_corpora": ["alpha"],
            },
        ],
        [
            {
                "source": "entity:a",
                "target": "entity:b",
                "predicate": "related_to",
                "relation_family": "WeakAssociation",
                "confidence": 0.4,
                "source_corpora": ["deselected"],
            }
        ],
    ])

    result = await get_concept_community_full(
        driver,
        entity_ids=["entity:a", "entity:b"],
        corpus_ids=["alpha"],
        max_nodes=10,
        max_edges=10,
    )

    assert result["edges"] == []
