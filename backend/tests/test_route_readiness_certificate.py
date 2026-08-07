"""Route-aware readiness + certificate tests (P4, owner §4).

Pins:
1. the artifact census translates durable readiness counts into the exact
   vocabulary of ``ROUTE_ARTIFACT_REQUIREMENTS``;
2. one corpus can be vector/hierarchical ready while graph routes stay
   fail-closed (no universal boolean);
3. stale/blocked graph promotion jobs self-heal deterministically (§4.3);
4. the certificate carries every §4.4 release identity and per-route verdicts.
"""

from __future__ import annotations

import pytest

from services.control_plane.release_registry import ReleaseRegistryResolution
from services.ingestion import graph_promotion_jobs as gpj
from services.ingestion import route_readiness as rr


def _no_release(monkeypatch) -> None:
    def fail_closed(path=None):
        return ReleaseRegistryResolution(
            status="fail_closed", reason="zero_active_entries"
        )

    monkeypatch.setattr(
        "services.control_plane.release_registry.load_release_registry", fail_closed
    )


class _Col:
    """Minimal motor-style collection fake."""

    def __init__(self, handlers: dict):
        self._handlers = handlers

    def __getattr__(self, name):
        if name in self._handlers:
            return self._handlers[name]
        raise AttributeError(name)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def limit(self, _value):
        return self

    async def to_list(self, length=None):
        return self._rows[: length if length else len(self._rows)]


def _full_readiness_record() -> dict:
    return {
        "corpus_id": "corpus-1",
        "status": "fully_enriched",
        "stale": False,
        "documents": {
            "total": 2,
            "queryable": 2,
            "lexicon_ready": 2,
            "lexicon_tracked": 2,
        },
        "chunks": {"total": 10},
        "summaries": {
            "retrieval_parent_done": 5,
            "document_done": 2,
            "summary_tree_index_ready": 2,
        },
        "graph": {"promoted": 2},
    }


def _db_fixture(*, extraction_rows: int = 3, jobs=None, doc=None):
    state = {"bulk_ops": [], "certificate": None}

    async def readiness_find_one(_query, _projection=None):
        return _full_readiness_record()

    async def extractions_count(_query):
        return extraction_rows

    async def extractions_find_one(_query, _projection=None, sort=None):
        return {
            "local_extraction": {
                "extractor_engine": "relex_local",
                "extractor_release": "relex_local.v1",
                "model_id": "knowledgator/gliner-relex-large-v1.0",
                "model_hash": "sha256:abc",
                "model_hash_verified": True,
                "schema_version": "polymath.extract.relex_local.v1",
                "ontology_hash": "sha256:onto",
                "acceptance_policy_hash": "sha256:policy",
            }
        }

    async def corpora_find_one(_query, _projection=None):
        return {
            "corpus_id": "corpus-1",
            "name": "Fixture",
            "generation": 1,
            "default_ingestion_config": {
                "embedding_model_id": "qwen3-embedding-0.6b-v1",
                "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
                "embedding_dimension": 1024,
                "embed_mode": "local",
            },
        }

    def graph_jobs_find(_query, _projection=None):
        return _Cursor(jobs or [])

    async def graph_jobs_bulk_write(ops, ordered=False):
        state["bulk_ops"].extend(ops)
        return None

    async def documents_find_one(_query, _projection=None):
        return doc

    async def certificates_replace_one(_filter, doc_, upsert=False):
        state["certificate"] = doc_
        return None

    db = {
        "corpus_readiness": _Col({"find_one": readiness_find_one}),
        "ghost_b_extractions": _Col(
            {"count_documents": extractions_count, "find_one": extractions_find_one}
        ),
        "corpora": _Col({"find_one": corpora_find_one}),
        "graph_promotion_jobs": _Col(
            {"find": graph_jobs_find, "bulk_write": graph_jobs_bulk_write}
        ),
        "documents": _Col({"find_one": documents_find_one}),
        "corpus_certificates": _Col({"replace_one": certificates_replace_one}),
    }
    return db, state


# ── census + route decisions ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_census_translates_readiness_into_route_artifacts(monkeypatch):
    _no_release(monkeypatch)
    db, _ = _db_fixture(extraction_rows=3)

    census = await rr.corpus_artifact_census(db, "corpus-1")

    assert set(census["present"]) == {
        "child_vectors",
        "parent_records",
        "summary_records",
        "evidence_obligations",
        "entity_lexicon",
        "extraction_artifact",
        "graph_projection",
    }
    assert census["release_pin"] is None
    assert census["release_reason"] == "zero_active_entries"


@pytest.mark.asyncio
async def test_route_decisions_split_vector_hierarchical_and_graph(monkeypatch):
    """The §4.2 invariant: vector_ready + hierarchical_ready while
    graph_not_ready — never one universal boolean."""

    _no_release(monkeypatch)
    db, _ = _db_fixture(extraction_rows=3)

    for route in ("vector_search", "hybrid_search", "curated_chat", "entity_lookup"):
        decision = await rr.decide_corpus_route(db, "corpus-1", route)
        assert decision.allowed is True, route
        assert decision.mode == "full", route
        assert decision.missing_artifacts == (), route

    graph_read = await rr.decide_corpus_route(db, "corpus-1", "graph_read")
    assert graph_read.allowed is False
    assert graph_read.mode == "partial"  # projection present, release absent
    assert graph_read.missing_artifacts == ("release_pin_match",)

    graph_write = await rr.decide_corpus_route(db, "corpus-1", "graph_write")
    assert graph_write.allowed is False
    assert graph_write.mode == "blocked"
    assert "release_bundle_absent" in graph_write.missing_artifacts


@pytest.mark.asyncio
async def test_route_readiness_payload_never_raises(monkeypatch):
    _no_release(monkeypatch)
    db, _ = _db_fixture(extraction_rows=0)

    payload = await rr.route_readiness_payload(db, ["corpus-1"], "hybrid_search")
    assert payload["route"] == "hybrid_search"
    assert payload["decisions"]["corpus-1"]["corpus_id"] == "corpus-1"


# ── §4.3 self-healing graph jobs ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_graph_job_becomes_noop_when_gap_resolved(monkeypatch):
    async def no_candidates(*_args, **_kwargs):
        return []

    monkeypatch.setattr(gpj, "_candidate_rows", no_candidates)
    jobs = [{"job_id": "graph_promote_aaa", "doc_id": "doc-1", "status": "queued"}]
    doc = {
        "write_state": {"qdrant_written": True, "neo4j_written": True, "verified": True},
        "ingestion_config": {"use_neo4j": True},
        "ingest_stage": "complete",
    }
    db, state = _db_fixture(jobs=jobs, doc=doc)

    result = await gpj.reevaluate_stale_graph_promotion_jobs(db, corpus_id="corpus-1")

    assert result == {"reevaluated": 1, "requeued": 0, "resolved": 1, "still_blocked": 0}
    update = state["bulk_ops"][0]._doc
    assert update["$set"]["status"] == "noop"
    assert update["$set"]["noop_reason"] == "graph_gap_resolved"


@pytest.mark.asyncio
async def test_blocked_graph_job_requeues_when_extractions_appear(monkeypatch):
    """A blocked dependency is reconsidered automatically when the required
    artifact appears."""

    candidate = {
        "job_id": "graph_promote_bbb",
        "doc_id": "doc-2",
        "status": "queued",
        "reason": "neo4j_missing",
        "staged_extractions": 4,
    }

    async def with_candidate(*_args, **_kwargs):
        return [candidate]

    monkeypatch.setattr(gpj, "_candidate_rows", with_candidate)
    jobs = [
        {
            "job_id": "graph_promote_bbb",
            "doc_id": "doc-2",
            "status": "blocked_no_extractions",
        }
    ]
    db, state = _db_fixture(jobs=jobs, doc=None)

    result = await gpj.reevaluate_stale_graph_promotion_jobs(db, corpus_id="corpus-1")

    assert result == {"reevaluated": 1, "requeued": 1, "resolved": 0, "still_blocked": 0}
    update = state["bulk_ops"][0]._doc
    assert update["$set"]["status"] == "queued"
    assert update["$set"]["reevaluation_reason"] == "dependency_reconsidered"


@pytest.mark.asyncio
async def test_blocked_graph_job_stays_blocked_without_artifact(monkeypatch):
    async def no_candidates(*_args, **_kwargs):
        return []

    monkeypatch.setattr(gpj, "_candidate_rows", no_candidates)
    jobs = [
        {
            "job_id": "graph_promote_ccc",
            "doc_id": "doc-3",
            "status": "blocked_no_extractions",
        }
    ]
    doc = {
        # Real gap remains: neo4j never written.
        "write_state": {"qdrant_written": True, "neo4j_written": False},
        "ingestion_config": {"use_neo4j": True},
        "ingest_stage": "complete",
    }
    db, state = _db_fixture(jobs=jobs, doc=doc)

    result = await gpj.reevaluate_stale_graph_promotion_jobs(db, corpus_id="corpus-1")

    assert result == {"reevaluated": 1, "requeued": 0, "resolved": 0, "still_blocked": 1}
    assert state["bulk_ops"] == []


# ── §4.4 certificate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_certificate_carries_every_release_and_route_verdict(monkeypatch):
    _no_release(monkeypatch)
    db, state = _db_fixture(extraction_rows=3)

    certificate = await rr.materialize_corpus_certificate(db, "corpus-1")

    assert certificate["certificate_id"].startswith("cert_")
    assert certificate["corpus_generation"] == 1
    assert certificate["graphify_release"]["extractor_engine"] == "graphify_cpu"
    assert certificate["graphify_release"]["model_id"] == "fastino/gliner2-base-v1"
    assert certificate["graphify_release"]["provider_release_hash"]
    assert certificate["ontology_release"]["reducer_release"]
    assert certificate["acceptance_policy_release"]["relation_release"]
    assert certificate["summary_algorithm_release"] == {
        "product": "deterministic_summary.v1",
        "model": "deterministic:v1",
        "provider_required": False,
    }
    assert certificate["embedding_release"]["embedding_model_id"] == (
        "qwen3-embedding-0.6b-v1"
    )
    assert certificate["qdrant_projection_release"]["rebuildable"] is True
    assert certificate["graph_projection_release"]["active_release"] is False

    routes = certificate["route_readiness"]
    assert set(routes) == set(rr.CERTIFICATE_ROUTES)
    assert routes["vector_search"]["allowed"] is True
    assert routes["graph_read"]["allowed"] is False
    assert routes["graph_write"]["allowed"] is False
    assert state["certificate"] == certificate
