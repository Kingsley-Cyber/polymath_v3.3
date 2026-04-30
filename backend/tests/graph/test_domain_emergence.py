"""
P1 verification — domain emergence pipeline.

Unit tests run by default and use stubs.
Integration test requires `pytest -m integration` AND an env var
`POLYMATH_TEST_CORPUS_ID` pointing at a real corpus with ingested docs.
"""
from __future__ import annotations

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from services.graph.analytics import (
    DomainCluster,
    DomainMap,
    cluster_docs,
    compute_corpus_change_signature,
    _dense_vector_from_qdrant,
    _deserialize_domain_map,
    label_clusters,
    top_entities_per_cluster,
)


# ── Unit tests (default) ────────────────────────────────────────────────────

def test_cluster_docs_empty_returns_empty():
    assert cluster_docs({}) == {}


def test_dense_vector_from_qdrant_supports_named_vectors():
    assert _dense_vector_from_qdrant([0.1, 0.2]) == [0.1, 0.2]
    assert _dense_vector_from_qdrant({"dense": [0.1, 0.2], "sparse": {"indices": [1]}}) == [0.1, 0.2]
    assert _dense_vector_from_qdrant({"dense": {"vector": [0.5, 0.6]}}) == [0.5, 0.6]
    assert _dense_vector_from_qdrant({"default": {"vector": [0.3, 0.4]}}) == [0.3, 0.4]
    assert _dense_vector_from_qdrant({"sparse": {"indices": [1], "values": [1.0]}}) is None


def test_cluster_docs_tiny_corpus_single_bucket():
    """Fewer than MIN_CLUSTER_SIZE * 2 docs → all into one cluster."""
    fps = {f"doc_{i}": list(np.random.rand(32)) for i in range(6)}
    result = cluster_docs(fps)
    assert set(result.values()) == {0}
    assert len(result) == 6


def test_cluster_docs_three_synthetic_clusters():
    """Three clumps on distinct regions of the unit sphere should split
    into ≥2 clusters (matches how real sentence-embedding fingerprints
    cluster after we L2-normalize them inside cluster_docs)."""
    rng = np.random.default_rng(42)
    fps: dict[str, list[float]] = {}
    dim = 32

    # Well-separated cluster directions: one-hot-like orientations spread
    # across different subspaces, each offset away from the origin.
    centers = []
    for axis in (0, 10, 20):
        c = np.zeros(dim, dtype=np.float32)
        c[axis:axis + 5] = 1.0  # non-overlapping active dims per cluster
        centers.append(c)

    for ci, center in enumerate(centers):
        for j in range(8):
            # Small jitter keeps vectors inside the cluster's cone.
            fps[f"c{ci}_d{j}"] = (center + rng.normal(0, 0.05, dim)).tolist()

    result = cluster_docs(fps)
    non_outlier = [c for c in result.values() if c != -1]
    # Majority should be assigned (not flagged as noise).
    assert len(non_outlier) >= 18, f"too many outliers: {result}"
    unique_clusters = set(non_outlier)
    assert len(unique_clusters) >= 2, (
        f"Expected ≥2 clusters from 3 separated clumps, got {len(unique_clusters)}: {result}"
    )
    # Each clump's assigned docs should concentrate in one bucket.
    for ci in range(3):
        assignments = [
            result[f"c{ci}_d{j}"] for j in range(8) if result[f"c{ci}_d{j}"] != -1
        ]
        if len(assignments) >= 4:  # skip clumps where too many landed in noise
            dominant = max(set(assignments), key=assignments.count)
            matching = sum(1 for a in assignments if a == dominant)
            assert matching / len(assignments) >= 0.75, (
                f"Clump {ci} too scattered: {assignments}"
            )


@pytest.mark.asyncio
async def test_compute_corpus_change_signature_deterministic():
    ts = datetime(2026, 4, 22, 12, 0, 0)
    fake_docs = [
        {"doc_id": "doc_b", "updated_at": ts},
        {"doc_id": "doc_a", "updated_at": ts},
    ]

    class _Cursor:
        def __init__(self, docs):
            self._docs = docs
        def sort(self, *_args, **_kwargs):
            return self
        async def to_list(self, length=None):
            # Mimic the sort by doc_id asc.
            return sorted(self._docs, key=lambda d: d["doc_id"])

    class _DocsCol:
        def find(self, *_a, **_kw):
            return _Cursor(fake_docs)

    db = {"documents": _DocsCol()}
    sig1 = await compute_corpus_change_signature(db, "cid")
    sig2 = await compute_corpus_change_signature(db, "cid")
    assert sig1 == sig2
    assert len(sig1) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_top_entities_per_cluster_no_driver_returns_empty():
    assignments = {"doc_a": 0, "doc_b": 0, "doc_c": 1}
    result = await top_entities_per_cluster(None, assignments)
    assert result == {0: [], 1: []}


@pytest.mark.asyncio
async def test_label_clusters_fallback_when_llm_fails(monkeypatch):
    """LLM raises → labeler returns placeholder names, never crashes."""
    import services.llm as llm_module

    async def _boom(**kwargs):
        raise RuntimeError("upstream offline")

    monkeypatch.setattr(llm_module.llm_service, "complete_sync", _boom)

    labels = await label_clusters(
        {0: ["Flutter", "Widget", "Dart"], 1: ["Prior", "Posterior", "Bayes"], -1: []}
    )
    assert labels[0] == "Cluster 0"
    assert labels[1] == "Cluster 1"
    assert labels[-1] == "Outliers"


@pytest.mark.asyncio
async def test_label_clusters_uses_llm_output(monkeypatch):
    import services.llm as llm_module

    async def _fake(**kwargs):
        return '{"0": "Flutter UI Development", "1": "Bayesian Inference"}'

    monkeypatch.setattr(llm_module.llm_service, "complete_sync", _fake)

    labels = await label_clusters(
        {0: ["Flutter", "Widget"], 1: ["Prior", "Posterior"]},
        model_override="test/model",
    )
    assert labels[0] == "Flutter UI Development"
    assert labels[1] == "Bayesian Inference"


def test_deserialize_domain_map_roundtrip():
    doc = {
        "corpus_id": "cid",
        "corpus_change_signature": "sig",
        "computed_at": datetime(2026, 4, 22, 0, 0, 0),
        "doc_assignments": {"doc_a": {"cluster_id": 0, "cluster_name": "X", "confidence": 1.0}},
        "clusters": {
            "0": {
                "cluster_id": 0,
                "name": "X",
                "size": 1,
                "top_entities": ["e1"],
                "centroid": [0.1, 0.2],
            }
        },
        "outliers": [],
    }
    dm = _deserialize_domain_map(doc)
    assert isinstance(dm, DomainMap)
    assert dm.corpus_id == "cid"
    assert 0 in dm.clusters
    assert dm.clusters[0].name == "X"
    assert dm.clusters[0].top_entities == ["e1"]


# ── Integration test (opt-in) ───────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_emerge_domains_integration():
    """Live end-to-end against the docker-compose stack.

    Requires env var `POLYMATH_TEST_CORPUS_ID` set to a corpus_id that has
    ingested documents. Skipped unless both the marker and the env are present.
    """
    corpus_id = os.environ.get("POLYMATH_TEST_CORPUS_ID")
    if not corpus_id:
        pytest.skip("POLYMATH_TEST_CORPUS_ID not set")

    from motor.motor_asyncio import AsyncIOMotorClient
    from qdrant_client import AsyncQdrantClient
    from neo4j import AsyncGraphDatabase

    from config import get_settings
    from services.graph.analytics import emerge_domains

    settings = get_settings()

    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL)
    neo4j = None
    if getattr(settings, "NEO4J_ENABLED", False):
        neo4j = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )

    try:
        dm = await emerge_domains(qdrant, neo4j, db, corpus_id, force=True)

        assert dm.corpus_id == corpus_id
        assert len(dm.doc_assignments) > 0, "no docs assigned to any cluster"
        assert len(dm.clusters) >= 1
        # All non-outlier clusters should have names.
        for cid, cluster in dm.clusters.items():
            if cid != -1:
                assert cluster.name, f"cluster {cid} missing name"

        # Second call hits the cache and returns the same payload.
        dm2 = await emerge_domains(qdrant, neo4j, db, corpus_id, force=False)
        assert dm2.corpus_change_signature == dm.corpus_change_signature
        assert set(dm2.doc_assignments.keys()) == set(dm.doc_assignments.keys())

    finally:
        await qdrant.close()
        if neo4j:
            await neo4j.close()
        mongo.close()
