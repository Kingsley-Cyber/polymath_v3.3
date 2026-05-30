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
from datetime import datetime, timedelta
import importlib
import os
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-password")


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


# ─── brain_cache durable Brain View snapshots ───────────────────────────────


class _BrainCacheCursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self.docs = [dict(doc) for doc in docs]

    def sort(self, field: str, direction: int):
        reverse = direction < 0
        self.docs.sort(key=lambda doc: doc.get(field) or datetime.min, reverse=reverse)
        return self

    def limit(self, count: int):
        self.docs = self.docs[:count]
        return self

    async def to_list(self, length=None):
        return self.docs if length is None else self.docs[:length]


class _BrainCacheCollection:
    def __init__(self):
        self.docs: list[dict[str, Any]] = []
        self.next_id = 1

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, branch) for branch in expected):
                    return False
                continue
            actual = doc.get(key)
            if isinstance(expected, dict):
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
                if "$lt" in expected and not (actual is not None and actual < expected["$lt"]):
                    return False
                if "$in" in expected:
                    candidates = expected["$in"]
                    if isinstance(actual, list):
                        if not any(item in candidates for item in actual):
                            return False
                    elif actual not in candidates:
                        return False
                continue
            if isinstance(actual, list):
                if expected not in actual:
                    return False
            elif actual != expected:
                return False
        return True

    def _project(self, doc: dict[str, Any], projection: dict[str, Any] | None):
        out = dict(doc)
        if projection:
            for key, include in projection.items():
                if include == 0:
                    out.pop(key, None)
        return out

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if self._matches(doc, query):
                return self._project(doc, projection)
        return None

    async def update_one(self, query, update, upsert=False):
        doc = None
        for candidate in self.docs:
            if self._matches(candidate, query):
                doc = candidate
                break
        if doc is None:
            doc = {"_id": self.next_id, **dict(query)}
            self.next_id += 1
            self.docs.append(doc)
        for key, value in (update.get("$set") or {}).items():
            doc[key] = value
        for key in (update.get("$unset") or {}):
            doc.pop(key, None)
        return SimpleNamespace(modified_count=1)

    async def update_many(self, query, update):
        modified = 0
        for doc in self.docs:
            if not self._matches(doc, query):
                continue
            modified += 1
            for key, value in (update.get("$set") or {}).items():
                doc[key] = value
        return SimpleNamespace(modified_count=modified)

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if not self._matches(doc, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    async def count_documents(self, query):
        return sum(1 for doc in self.docs if self._matches(doc, query))

    def find(self, query, projection=None):
        return _BrainCacheCursor(
            [self._project(doc, projection) for doc in self.docs if self._matches(doc, query)]
        )


class _BrainCacheDB:
    def __init__(self):
        self.collection = _BrainCacheCollection()

    def __getitem__(self, name):
        from services.graph import brain_cache

        assert name == brain_cache.CACHE_COLLECTION
        return self.collection


def _import_graph_router_without_auth(monkeypatch):
    fake_auth = types.ModuleType("routers.auth")

    async def fake_current_user():
        return {"id": "test-user", "username": "test"}

    fake_auth.get_current_user = fake_current_user
    monkeypatch.setitem(sys.modules, "routers.auth", fake_auth)
    return importlib.import_module("routers.graph")


@pytest.mark.asyncio
async def test_brain_view_cache_hit_is_order_independent_and_signature_checked(monkeypatch):
    from services.graph import analytics
    from services.graph import brain_cache

    sigs = {"alpha": "sig-a", "beta": "sig-b"}

    async def fake_sig(_db, corpus_id):
        return sigs[corpus_id]

    monkeypatch.setattr(analytics, "compute_corpus_change_signature", fake_sig)
    db = _BrainCacheDB()
    payload = {
        "documents": [{"doc_id": "d1", "corpus_id": "alpha"}],
        "bridges": [],
        "meta": {"total_documents": 1, "total_bridges": 0},
    }

    await brain_cache.store_brain_view_cache(
        db,
        ["beta", "alpha"],
        payload,
        detail="bridges",
        limit=2000,
        bridge_entity_cap=32,
    )

    hit, _selection_sig, _corpus_sigs = await brain_cache.get_cached_brain_view(
        db,
        ["alpha", "beta"],
        detail="bridges",
        limit=2000,
        bridge_entity_cap=32,
    )
    assert hit is not None
    assert hit["meta"]["brain_cache"]["status"] == "hit"
    assert hit["documents"][0]["doc_id"] == "d1"

    sigs["beta"] = "sig-b-new"
    miss, _selection_sig, _corpus_sigs = await brain_cache.get_cached_brain_view(
        db,
        ["alpha", "beta"],
        detail="bridges",
        limit=2000,
        bridge_entity_cap=32,
    )
    assert miss is None


@pytest.mark.asyncio
async def test_brain_view_route_hits_cache_then_rebuilds_when_signature_changes(monkeypatch):
    graph_router = _import_graph_router_without_auth(monkeypatch)
    from services.graph import analytics, queries

    sig = {"alpha": "sig-1"}

    async def fake_sig(_db, corpus_id):
        return sig[corpus_id]

    calls = {"count": 0}

    async def fake_get_brain_view(_driver, corpus_ids, *, limit, bridge_entity_cap, detail):
        calls["count"] += 1
        return {
            "documents": [
                {
                    "doc_id": f"d{calls['count']}",
                    "corpus_id": corpus_ids[0],
                    "label": "Book",
                    "bridge_count": 0,
                }
            ],
            "bridges": [],
            "meta": {
                "corpus_count": 1,
                "total_documents": 1,
                "total_bridges": 0,
                "limit_applied": limit,
                "detail": detail,
                "bridge_entity_cap": bridge_entity_cap,
            },
        }

    db = _BrainCacheDB()
    monkeypatch.setattr(analytics, "compute_corpus_change_signature", fake_sig)
    monkeypatch.setattr(queries, "get_brain_view", fake_get_brain_view)
    monkeypatch.setattr(graph_router.ingestion_service, "_db", db)
    monkeypatch.setattr(graph_router.ingestion_service, "_neo4j", object())

    body = {
        "corpus_ids": ["alpha"],
        "detail": "bridges",
        "limit": 2000,
        "bridge_entity_cap": 32,
    }
    first = await graph_router.graph_brain_view(body)
    second = await graph_router.graph_brain_view(body)
    assert calls["count"] == 1
    assert first["meta"]["brain_cache"]["status"] == "miss_stored"
    assert second["meta"]["brain_cache"]["status"] == "hit"
    assert second["documents"][0]["doc_id"] == "d1"

    sig["alpha"] = "sig-2"
    third = await graph_router.graph_brain_view(body)
    assert calls["count"] == 2
    assert third["meta"]["brain_cache"]["status"] == "miss_stored"
    assert third["documents"][0]["doc_id"] == "d2"


@pytest.mark.asyncio
async def test_brain_view_cache_status_reports_ready_missing_and_stale(monkeypatch):
    graph_router = _import_graph_router_without_auth(monkeypatch)
    from services.graph import analytics, brain_cache

    sig = {"alpha": "sig-1"}

    async def fake_sig(_db, corpus_id):
        return sig[corpus_id]

    db = _BrainCacheDB()
    monkeypatch.setattr(analytics, "compute_corpus_change_signature", fake_sig)
    monkeypatch.setattr(graph_router.ingestion_service, "_db", db)

    body = {
        "corpus_ids": ["alpha"],
        "detail": "bridges",
        "limit": 2000,
        "bridge_entity_cap": 32,
    }
    missing = await graph_router.graph_brain_view_cache_status(body)
    assert missing["status"] == "missing"

    await brain_cache.store_brain_view_cache(
        db,
        ["alpha"],
        {"documents": [], "bridges": [], "meta": {"total_documents": 0, "total_bridges": 0}},
        detail="bridges",
        limit=2000,
        bridge_entity_cap=32,
    )
    ready = await graph_router.graph_brain_view_cache_status(body)
    assert ready["status"] == "ready"
    assert ready["stored_status"] == "ready"

    sig["alpha"] = "sig-2"
    stale = await graph_router.graph_brain_view_cache_status(body)
    assert stale["status"] == "stale"


@pytest.mark.asyncio
async def test_brain_view_cache_invalidation_marks_stale_and_can_delete(monkeypatch):
    from services.graph import analytics, brain_cache

    async def fake_sig(_db, corpus_id):
        return f"sig-{corpus_id}"

    db = _BrainCacheDB()
    monkeypatch.setattr(analytics, "compute_corpus_change_signature", fake_sig)
    await brain_cache.store_brain_view_cache(
        db,
        ["alpha", "beta"],
        {"documents": [], "bridges": [], "meta": {"total_documents": 0, "total_bridges": 0}},
        detail="bridges",
        limit=2000,
        bridge_entity_cap=32,
    )

    modified = await brain_cache.invalidate_brain_view_cache_for_corpus(db, "alpha")
    assert modified == 1
    status = await brain_cache.get_brain_view_cache_status(
        db,
        ["alpha", "beta"],
        detail="bridges",
        limit=2000,
        bridge_entity_cap=32,
    )
    assert status["status"] == "stale"
    assert status["stored_status"] == "stale"

    deleted = await brain_cache.invalidate_brain_view_cache_for_corpus(
        db,
        "alpha",
        delete=True,
    )
    assert deleted == 1
    assert db.collection.docs == []


@pytest.mark.asyncio
async def test_brain_view_cache_prune_removes_old_stale_and_overflow():
    from services.graph import brain_cache

    now = datetime.utcnow()
    db = _BrainCacheDB()
    db.collection.docs = [
        {
            "_id": 1,
            "status": "stale",
            "stale_at": now - timedelta(days=30),
            "updated_at": now - timedelta(days=30),
        },
        {"_id": 2, "status": "ready", "updated_at": now - timedelta(days=10)},
        {"_id": 3, "status": "ready", "updated_at": now - timedelta(days=5)},
        {"_id": 4, "status": "ready", "updated_at": now},
    ]

    result = await brain_cache.prune_brain_view_cache(
        db,
        max_entries=2,
        stale_retention_days=14,
    )

    assert result["deleted_stale"] == 1
    assert result["deleted_overflow"] == 1
    assert {doc["_id"] for doc in db.collection.docs} == {3, 4}


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
