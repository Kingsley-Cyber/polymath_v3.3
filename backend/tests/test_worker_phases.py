"""
Worker phase-order, resume-gate, mode-toggle, and concurrency tests.

Companion to test_ghost_b_staging.py — that file owns the Ghost B staging
round-trip and resume skip path. Here we exercise:
  - phase ordering across Deep / Balanced / Fast modes
  - Ghost A resume-from-Mongo skip path
  - hard-abort behavior on Ghost A / Ghost B partial failures
  - per-corpus concurrency preservation (no env fallback on the hot path)
  - universal schema still applies on a Balanced-flag IngestionConfig

All unit tests mock at the writer / ghost / embedder boundary so Mongo,
Qdrant, Neo4j clients are never contacted. The single integration test is
guarded by @pytest.mark.integration and is skipped in default runs.
"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.schemas import (
    IngestionConfig,
    ModelProfileRef,
    SourceTier,
    WriteState,
)
from services.ghost_a import SummaryResult
from services.ghost_b import (
    EntityItem,
    ExtractionBatchReport,
    ExtractionResult,
    RelationItem,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
)
from services.ingestion import worker
from services.ingestion.worker import (
    GhostAFailure,
    GhostBFailure,
    _build_ghost_pool,
    _select_ghost_b_extraction_policy,
    _select_high_signal_children,
)


# ── Test data builders ──────────────────────────────────────────────────────


def _parse_result(tier: SourceTier = SourceTier.tier_a) -> Any:
    return SimpleNamespace(
        text="Apple Inc. hired Steve Jobs in 1976.",
        markdown="# Section\n\nApple Inc. hired Steve Jobs in 1976.",
        sections=[],
        pages=None,
        has_structure=True,
        source_tier=tier,
        h1_count=1,
        h2_count=0,
        num_pages=1,
        source_format="md",
        augmented_with_synthetic_headers=False,
        injected_headers_audit=[],
    )


def _parent(doc_id: str, corpus_id: str, pid: str = "p0", child_id: str = "c0") -> Any:
    child = SimpleNamespace(
        chunk_id=child_id,
        parent_id=pid,
        doc_id=doc_id,
        corpus_id=corpus_id,
        text="Apple Inc. hired Steve Jobs in 1976.",
        heading_path=["Section"],
        source_tier=SourceTier.tier_a.value,
        token_count=10,
    )
    parent = SimpleNamespace(
        parent_id=pid,
        doc_id=doc_id,
        corpus_id=corpus_id,
        text="Apple Inc. hired Steve Jobs in 1976.",
        heading_path=["Section"],
        source_tier=SourceTier.tier_a.value,
        children=[child],
    )
    return parent, child


def _fake_extraction_result(
    chunk_id: str, doc_id: str, corpus_id: str
) -> ExtractionResult:
    return ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id=chunk_id,
        doc_id=doc_id,
        corpus_id=corpus_id,
        entities=[EntityItem("apple inc", "Apple Inc.", "Organization", 0.95)],
        relations=[
            RelationItem("apple inc", "created_by", "steve jobs", "entity", 0.9)
        ],
    )


def _fake_summary_result(parent_id: str, doc_id: str, corpus_id: str) -> SummaryResult:
    return SummaryResult(
        parent_id=parent_id,
        doc_id=doc_id,
        corpus_id=corpus_id,
        source_tier=SourceTier.tier_a.value,
        summary="A compact summary.",
    )


def test_graph_status_token_budget_failure_is_not_retry_backfill():
    assert (
        worker._graph_status_after_extraction(
            ghost_b_out=[],
            ghost_b_failures=[],
            ghost_b_metrics={
                "requested_chunks": 2,
                "extracted_chunks": 0,
                "failed_chunk_count": 2,
                "retryable_failed_chunks": 0,
                "error_counts": {"token_budget": 2},
            },
        )
        == worker.GRAPH_FAILED_TOKEN_BUDGET
    )


# ── Mock harness ────────────────────────────────────────────────────────────


class PhaseRecorder:
    """Collects ordered phase tags from mocked boundary calls."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def tag(self, name: str):
        def _mk(ret_value=None, *, is_async: bool = True):
            async def _async(*args, **kwargs):
                self.events.append(name)
                return ret_value
            def _sync(*args, **kwargs):
                self.events.append(name)
                return ret_value
            return _async if is_async else _sync
        return _mk


def _install_mocks(
    recorder: PhaseRecorder,
    *,
    parents: list,
    children: list,
    summaries: list | None,
    ghost_b_out: list | None,
    existing_doc: dict | None = None,
):
    """Patch every worker-boundary import, recording phase order in `recorder`.
    Returns a dict of the relevant mock handles for per-test assertions.
    """
    doc_id = "deadbeef" * 8

    parse_mock = AsyncMock(
        side_effect=recorder.tag("parse")(
            ret_value=_parse_result(), is_async=True
        )
    )
    chunk_mock = MagicMock(
        side_effect=recorder.tag("chunk")(
            ret_value=(parents, children, []), is_async=False
        )
    )

    async def _ghosts_side_effect(**kwargs):
        if kwargs.get("include_ghost_b") is False:
            recorder.events.append("ghost_a")
            return worker.GhostRunResult(
                summaries=summaries,
                ghost_b_out=None,
            )
        if kwargs.get("include_ghost_a") is False:
            recorder.events.append("ghost_b")
            return worker.GhostRunResult(
                summaries=None,
                ghost_b_out=ghost_b_out,
                ghost_b_metrics=worker._ghost_b_metrics_for_skipped(ghost_b_out),
            )
        recorder.events.append("ghosts_parallel")
        return worker.GhostRunResult(
            summaries=summaries,
            ghost_b_out=ghost_b_out,
            ghost_b_metrics=worker._ghost_b_metrics_for_skipped(ghost_b_out),
        )

    run_ghosts_mock = AsyncMock(side_effect=_ghosts_side_effect)

    async def _mongo_write_side_effect(**kwargs):
        recorder.events.append("mongo_write")

    mongo_write_mock = AsyncMock(side_effect=_mongo_write_side_effect)

    async def _embed_side_effect(**kwargs):
        recorder.events.append("embed")
        vec_map = {c.chunk_id: [0.0] * 1024 for c in children}
        summary_vec_map = {
            s.parent_id: [0.0] * 1024 for s in (summaries or [])
        }
        return vec_map, summary_vec_map

    embed_mock = AsyncMock(side_effect=_embed_side_effect)

    async def _qdrant_side_effect(**kwargs):
        recorder.events.append("qdrant_write")

    qdrant_mock = AsyncMock(side_effect=_qdrant_side_effect)
    graph_vectors_mock = AsyncMock()

    async def _neo4j_side_effect(**kwargs):
        recorder.events.append("neo4j_write")

    neo4j_mock = AsyncMock(side_effect=_neo4j_side_effect)

    get_doc_mock = AsyncMock(return_value=existing_doc)
    # Phase 21 — worker now re-reads the live corpus at top of run_ingest_job
    # to pick up mutable fields (embed_mode / pools). A minimal stub body is
    # enough since all phases downstream are mocked.
    get_corpus_mock = AsyncMock(return_value={
        "corpus_id": "c" * 36,
        "default_ingestion_config": {},
    })
    update_state_mock = AsyncMock()
    upsert_progress_doc_mock = AsyncMock()
    mongo_db = MagicMock()
    # For the corpora counter update path in run_ingest_job
    mongo_db.__getitem__.return_value.update_one = AsyncMock()

    patches = [
        patch.object(worker.docling_adapter, "parse_document", parse_mock),
        patch.object(worker.tier_chunker, "chunk", chunk_mock),
        patch.object(worker, "_run_ghosts_parallel", run_ghosts_mock),
        patch.object(worker, "_write_mongo_all", mongo_write_mock),
        patch.object(worker, "_embed_batch_for_doc", embed_mock),
        patch.object(worker, "_write_qdrant_for_doc", qdrant_mock),
        patch.object(worker, "_write_graph_vectors_for_doc", graph_vectors_mock),
        patch.object(worker, "_write_neo4j_for_doc", neo4j_mock),
        patch.object(worker.mongo_reader, "get_document", get_doc_mock),
        patch.object(worker.mongo_reader, "get_corpus", get_corpus_mock),
        patch.object(worker.mongo_writer, "update_write_state", update_state_mock),
        patch.object(worker.mongo_writer, "upsert_document", upsert_progress_doc_mock),
        patch.object(worker.settings, "NEO4J_ENABLED", True),
    ]
    for p in patches:
        p.start()

    def _stop_all():
        for p in patches:
            p.stop()

    return {
        "doc_id": doc_id,
        "parse": parse_mock,
        "chunk": chunk_mock,
        "ghosts": run_ghosts_mock,
        "mongo_write": mongo_write_mock,
        "embed": embed_mock,
        "qdrant": qdrant_mock,
        "graph_vectors": graph_vectors_mock,
        "neo4j": neo4j_mock,
        "get_doc": get_doc_mock,
        "update_state": update_state_mock,
        "upsert_progress_doc": upsert_progress_doc_mock,
        "db": mongo_db,
        "stop_all": _stop_all,
    }


async def _run_job(mocks, config: IngestionConfig, *, corpus_id: str = "c" * 36):
    return await worker.run_ingest_job(
        job_id="job-1",
        data=b"dummy bytes",
        filename="doc.txt",
        corpus_id=corpus_id,
        user_id="u1",
        ingestion_config=config,
        db=mocks["db"],
        qdrant_client=MagicMock(),
        neo4j_driver=MagicMock(),
        model="ollama/qwen3:1.7b",
    )


# ── Phase order tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_order_deep_mode():
    """Deep mode: all seven phases fire in the locked order."""
    rec = PhaseRecorder()
    # Build parents/children with stable ids — worker's doc_id is content-hashed
    # but since we mock parse_document, the chunker returns these directly.
    p, c = _parent("stub-doc", "c" * 36)
    summaries = [_fake_summary_result(p.parent_id, "stub-doc", "c" * 36)]
    ghost_b_out = [_fake_extraction_result(c.chunk_id, "stub-doc", "c" * 36)]
    m = _install_mocks(rec, parents=[p], children=[c],
                       summaries=summaries, ghost_b_out=ghost_b_out)
    try:
        cfg = IngestionConfig(
            use_neo4j=True, chunk_summarization=True,
            target_qdrant_collections=["naive", "hrag", "graph"],
        )
        result = await _run_job(m, cfg)
        assert result.status == "done"
        assert rec.events == [
            "parse", "chunk", "ghost_a",
            "mongo_write", "embed", "qdrant_write", "ghost_b", "neo4j_write",
        ]
        trace = m["mongo_write"].await_args.kwargs["decision_trace"]
        assert trace["chunking_strategy"] == "heading_bound"
        assert trace["graph_strategy"] == "full_ontology"
    finally:
        m["stop_all"]()


@pytest.mark.asyncio
async def test_phase_order_fast_mode():
    """Fast mode: both ghosts disabled, Neo4j branch skipped entirely."""
    rec = PhaseRecorder()
    p, c = _parent("stub-doc", "c" * 36)
    # The Ghost A wrapper still runs, but both branches are effectively no-ops.
    # The Neo4j phase should NOT call write_document_graph.
    m = _install_mocks(rec, parents=[p], children=[c],
                       summaries=None, ghost_b_out=None)
    try:
        cfg = IngestionConfig(
            use_neo4j=False, chunk_summarization=False,
            target_qdrant_collections=["naive", "hrag"],
        )
        result = await _run_job(m, cfg)
        assert result.status == "done"
        assert "neo4j_write" not in rec.events
        # Mongo + Qdrant still run; graph enrichment is skipped.
        assert rec.events == [
            "parse", "chunk", "ghost_a",
            "mongo_write", "embed", "qdrant_write",
        ]
    finally:
        m["stop_all"]()


@pytest.mark.asyncio
async def test_phase_order_balanced_mode():
    """Balanced: Ghost B runs (use_neo4j), Ghost A doesn't (no summarization)."""
    rec = PhaseRecorder()
    p, c = _parent("stub-doc", "c" * 36)
    ghost_b_out = [_fake_extraction_result(c.chunk_id, "stub-doc", "c" * 36)]
    # summaries=None → _write_mongo_all inlines None summary on parents,
    # _embed_batch_for_doc has no summary texts, _write_qdrant_for_doc
    # skips upsert_summaries. All handled by the downstream helpers (mocked).
    m = _install_mocks(rec, parents=[p], children=[c],
                       summaries=None, ghost_b_out=ghost_b_out)
    try:
        cfg = IngestionConfig(
            use_neo4j=True, chunk_summarization=False,
            target_qdrant_collections=["naive", "hrag", "graph"],
        )
        await _run_job(m, cfg)
        assert rec.events == [
            "parse", "chunk", "ghost_a",
            "mongo_write", "embed", "qdrant_write", "ghost_b", "neo4j_write",
        ]
        # Ghost A off, Ghost B on — summaries arg to the mongo write was None.
        kwargs = m["mongo_write"].await_args.kwargs
        assert kwargs["summaries"] is None
        assert kwargs["ghost_b_out"] is None
    finally:
        m["stop_all"]()


# ── Resume-gate tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ghost_a_skip_on_retry_reads_mongo_summaries():
    """When parent_chunks[].summary is populated and mongo_written=True but
    qdrant_written=False, _run_ghosts_parallel reconstructs summaries from
    Mongo without invoking summarize_parents."""
    doc_id, corpus_id = "doc-resume-a", "c" * 36
    parents = [_parent(doc_id, corpus_id)[0]]
    children = [parents[0].children[0]]

    existing = {
        "doc_id": doc_id, "corpus_id": corpus_id,
        "write_state": {"mongo_written": True, "qdrant_written": False,
                        "neo4j_written": False},
        "parent_chunks": [{
            "parent_id": parents[0].parent_id,
            "summary": "Previously computed summary from a prior run.",
        }],
    }
    ws = WriteState(**existing["write_state"])
    cfg = IngestionConfig(
        use_neo4j=False, chunk_summarization=True,
        target_qdrant_collections=["naive", "hrag"],
    )

    summarize_mock = AsyncMock()  # should NOT be called
    with patch.object(worker, "summarize_parents", summarize_mock), \
         patch.object(worker.settings, "NEO4J_ENABLED", True):
        summaries, ghost_b_out = await worker._run_ghosts_parallel(
            config=cfg, parents=parents, children=children,
            doc_id=doc_id, corpus_id=corpus_id,
            model="ollama/qwen3:1.7b",
            db=MagicMock(), qdrant_client=MagicMock(),
            neo4j_driver=MagicMock(),
            existing_doc=existing, ws=ws,
        )

    assert summarize_mock.await_count == 0, "Ghost A LLM must not be invoked on resume"
    assert summaries is not None
    assert len(summaries) == 1
    assert summaries[0].summary == "Previously computed summary from a prior run."
    assert ghost_b_out is None  # use_neo4j off


@pytest.mark.asyncio
async def test_ghost_a_partial_reconstruct_falls_back_to_llm():
    """Missing summary on even one parent = partial reconstruct, must re-run
    ghost_a so the LLM fills the gap coherently."""
    doc_id, corpus_id = "doc-partial-a", "c" * 36
    p1, _ = _parent(doc_id, corpus_id, pid="p0", child_id="c0")
    p2, _ = _parent(doc_id, corpus_id, pid="p1", child_id="c1")
    parents = [p1, p2]
    children = p1.children + p2.children

    existing = {
        "doc_id": doc_id, "corpus_id": corpus_id,
        "write_state": {"mongo_written": True, "qdrant_written": False,
                        "neo4j_written": False},
        # Only p0 has a summary; p1 is blank → partial reconstruct → re-run.
        "parent_chunks": [
            {"parent_id": "p0", "summary": "First summary."},
            {"parent_id": "p1", "summary": ""},
        ],
    }
    ws = WriteState(**existing["write_state"])
    cfg = IngestionConfig(
        use_neo4j=False, chunk_summarization=True,
        target_qdrant_collections=["naive", "hrag"],
    )

    async def _fake_summarize(tasks, **kwargs):
        return [_fake_summary_result(t.parent_id, doc_id, corpus_id) for t in tasks]

    summarize_mock = AsyncMock(side_effect=_fake_summarize)
    with patch.object(worker, "summarize_parents", summarize_mock), \
         patch.object(worker.settings, "NEO4J_ENABLED", True):
        summaries, _ = await worker._run_ghosts_parallel(
            config=cfg, parents=parents, children=children,
            doc_id=doc_id, corpus_id=corpus_id,
            model="m",
            db=MagicMock(), qdrant_client=MagicMock(),
            neo4j_driver=MagicMock(),
            existing_doc=existing, ws=ws,
        )
    assert summarize_mock.await_count == 1
    assert len(summaries) == 2


# ── Hard-abort tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ghost_a_total_outage_raises_and_skips_writes():
    """Ghost A returning ZERO summaries on a non-empty task list → GhostAFailure.
    This is the "provider outage" path: every lane is broken, no parent could
    be summarized at all. run_ingest_job must not write to Mongo / Qdrant /
    Neo4j, and write_state flags must remain at their pre-job values.

    Partial coverage (some parents skipped due to token budget) does NOT take
    this path — see test_ghost_a_partial_soft_warning_doc_proceeds."""
    rec = PhaseRecorder()
    p, c = _parent("stub-doc", "c" * 36)
    m = _install_mocks(rec, parents=[p], children=[c],
                       summaries=None, ghost_b_out=None)
    # Replace the mocked _run_ghosts_parallel with one that raises.
    async def _raise(**kw):
        rec.events.append("ghosts_parallel")
        raise GhostAFailure("Ghost A produced 0/1 summaries")
    worker._run_ghosts_parallel = _raise  # type: ignore[assignment]
    try:
        cfg = IngestionConfig(use_neo4j=False, chunk_summarization=True)
        with pytest.raises(GhostAFailure):
            await _run_job(m, cfg)
        # No post-ghost phase fired
        for phase in ("mongo_write", "embed", "qdrant_write", "neo4j_write"):
            assert phase not in rec.events
        # No write_state flag flips; only the retry-visible Ghost A warning is
        # persisted on the progress document before the hard abort propagates.
        assert m["update_state"].await_count == 1
        _, kwargs = m["update_state"].await_args
        assert kwargs["warnings"][0].startswith("Ghost A summarization failed:")
    finally:
        m["stop_all"]()


@pytest.mark.asyncio
async def test_ghost_a_partial_soft_warning_doc_proceeds():
    """Partial Ghost A coverage (e.g. 1 of 2 parents summarized because the
    other was skipped by the token-budget guard) is a warning, not a failure.

    Document must commit to Mongo + Qdrant with whichever summaries succeeded.
    The partial warning surfaces on write_state.warnings so the UI shows it,
    but vector_ready=True (the doc is queryable). Mirrors Ghost B's existing
    soft-fail behavior — a few oversized parents must not kill an ingest of
    a 160-parent book."""
    rec = PhaseRecorder()
    p1, c1 = _parent("stub-doc", "c" * 36, pid="p0", child_id="c0")
    p2, c2 = _parent("stub-doc", "c" * 36, pid="p1", child_id="c1")
    # Only one summary returned for two parents — the other was skipped by
    # ghost_a's _safe_summary_budget guard (oversized parent text).
    summaries = [_fake_summary_result("p0", "stub-doc", "c" * 36)]
    m = _install_mocks(rec, parents=[p1, p2], children=[c1, c2],
                       summaries=summaries, ghost_b_out=None)
    # Override the mocked ghost runner to also surface the partial warning,
    # exactly like the real _a_branch does after my soft-fail edit.
    async def _ghosts_with_partial_warning(**kw):
        rec.events.append("ghosts_parallel")
        return worker.GhostRunResult(
            summaries=summaries,
            ghost_b_out=None,
            warnings=[
                worker._ghost_a_partial_warning(summarized=1, total=2),
            ],
        )
    worker._run_ghosts_parallel = _ghosts_with_partial_warning  # type: ignore[assignment]
    try:
        cfg = IngestionConfig(
            use_neo4j=False, chunk_summarization=True,
            target_qdrant_collections=["naive", "hrag"],
        )
        result = await _run_job(m, cfg)
        # Doc commits successfully despite Ghost A partial.
        assert result.status == "done"
        assert result.write_state.vector_ready is True
        # All post-ghost phases ran.
        for phase in ("mongo_write", "embed", "qdrant_write"):
            assert phase in rec.events
        # Warning is preserved on the document so the UI can display it.
        assert any(
            "Ghost A parent summarization partial" in w
            for w in (result.write_state.warnings or [])
        ), result.write_state.warnings
    finally:
        m["stop_all"]()


def test_ghost_a_partial_warning_message_format():
    """The partial-warning helper must produce a string that downstream UI
    can recognize and that includes the coverage ratio."""
    msg = worker._ghost_a_partial_warning(summarized=126, total=160)
    assert "126/160" in msg
    assert "partial" in msg.lower()
    # Must explicitly call out that vector RAG is unaffected — this is what
    # tells the user the ingest is still useful even with skipped parents.
    assert "vector RAG" in msg or "vector rag" in msg.lower()


@pytest.mark.asyncio
async def test_ghost_b_failure_after_vector_ready_marks_backfill_needed():
    """Ghost B failure after vector readiness is a graph warning, not a vector
    ingest failure."""
    rec = PhaseRecorder()
    p, c = _parent("stub-doc", "c" * 36)
    m = _install_mocks(rec, parents=[p], children=[c],
                       summaries=None, ghost_b_out=None)
    async def _raise(**kw):
        if kw.get("include_ghost_b") is False:
            rec.events.append("ghost_a")
            return worker.GhostRunResult(summaries=None, ghost_b_out=None)
        rec.events.append("ghost_b")
        raise GhostBFailure("Ghost B partial: 0/1")
    worker._run_ghosts_parallel = _raise  # type: ignore[assignment]
    try:
        cfg = IngestionConfig(use_neo4j=True, chunk_summarization=False,
                              target_qdrant_collections=["naive", "hrag", "graph"])
        result = await _run_job(m, cfg)
        assert result.status == "done"
        assert result.write_state.vector_ready is True
        assert result.write_state.graph_status == worker.GRAPH_NEEDS_BACKFILL
        assert "mongo_write" in rec.events
        assert "embed" in rec.events
        assert "qdrant_write" in rec.events
        assert "neo4j_write" not in rec.events
    finally:
        m["stop_all"]()


# ── Per-corpus concurrency ──────────────────────────────────────────────────


def test_per_corpus_concurrency_preserved_in_pool():
    """_build_ghost_pool must carry through each entry's max_concurrent
    verbatim — never substitute settings.SUMMARY_MAX_CONCURRENT or 1."""
    refs = [
        ModelProfileRef(provider_preset="openai", model="openai/gpt-4o",
                        max_concurrent=3),
        ModelProfileRef(provider_preset="ollama", model="ollama/qwen3:1.7b",
                        max_concurrent=7),
    ]
    pool = _build_ghost_pool(refs)
    assert [entry["max_concurrent"] for entry in pool] == [3, 7]
    # Raw dict input still works (ops / migration shape)
    pool2 = _build_ghost_pool([{"model": "x", "max_concurrent": 5}])
    assert pool2[0]["max_concurrent"] == 5


def test_build_ghost_pool_defaults_to_one_when_missing():
    """A missing / zero / None max_concurrent clamps to 1 — never blows up and
    never expands beyond what the user asked for."""
    pool = _build_ghost_pool([{"model": "x"}, {"model": "y", "max_concurrent": 0}])
    assert [e["max_concurrent"] for e in pool] == [1, 1]


def test_pre_vector_model_semaphore_isolates_local_from_cloud():
    local_sem = worker._pre_vector_model_semaphore(IngestionConfig(embed_mode="local"))
    cloud_sem = worker._pre_vector_model_semaphore(IngestionConfig(embed_mode="api"))
    modal_sem = worker._pre_vector_model_semaphore(IngestionConfig(embed_mode="modal"))

    assert local_sem is worker._LOCAL_MODEL_PHASE_SEMAPHORE
    assert cloud_sem is worker._CLOUD_MODEL_PHASE_SEMAPHORE
    assert modal_sem is worker._CLOUD_MODEL_PHASE_SEMAPHORE
    assert local_sem is not cloud_sem


def test_ghost_b_policy_uses_full_for_small_docs():
    cfg = IngestionConfig(
        large_doc_child_threshold=100,
        full_extract_max_children=100,
        compact_mode_max_entities=6,
        compact_mode_max_relations=6,
    )

    policy = _select_ghost_b_extraction_policy(
        cfg,
        total_children=12,
        body_children=10,
        skipped_low_value_by_kind={"toc": 2},
    )

    assert policy.extraction_strategy == "full_ontology"
    assert policy.extraction_mode == "full"
    assert policy.graph_completeness == "graph-complete"
    assert policy.full_extraction_chunks == 10
    assert policy.compact_extraction_chunks == 0
    assert policy.skipped_low_value_chunks == 2


def test_ghost_b_policy_uses_compact_for_large_docs():
    cfg = IngestionConfig(
        large_doc_child_threshold=100,
        full_extract_max_children=100,
        compact_mode_max_entities=6,
        compact_mode_max_relations=5,
    )

    policy = _select_ghost_b_extraction_policy(
        cfg,
        total_children=140,
        body_children=120,
        skipped_low_value_by_kind={"bibliography": 20},
    )

    assert policy.extraction_strategy == "compact_large_doc"
    assert policy.extraction_mode == "compact"
    assert policy.graph_completeness == "graph-compact"
    assert policy.compact_extraction_chunks == 120
    assert policy.full_extraction_chunks == 0
    assert policy.max_entities_per_chunk == 6
    assert policy.max_relations_per_chunk == 5
    assert policy.max_completion_tokens <= 2048
    assert policy.skipped_low_value_chunks == 20


def test_decision_trace_records_large_doc_compact_policy():
    cfg = IngestionConfig(
        use_neo4j=True,
        large_doc_child_threshold=100,
        full_extract_max_children=100,
        compact_mode_max_entities=6,
        compact_mode_max_relations=5,
    )
    parse = _parse_result(SourceTier.ocr_ast)
    parse.source_format = "pypdf_fast_text"
    parse.num_pages = 42
    parse.pages = ["body"] * 42
    parse.has_structure = False
    children = [
        SimpleNamespace(chunk_id=f"c{i}", chunk_kind=worker.ChunkKind.BODY)
        for i in range(120)
    ]
    children.extend(
        [
            SimpleNamespace(
                chunk_id=f"b{i}",
                chunk_kind=worker.ChunkKind.BIBLIOGRAPHY,
            )
            for i in range(3)
        ]
    )

    trace = worker._build_decision_trace(
        parse_result=parse,
        source_mime="application/pdf",
        filename="book.pdf",
        source_tier=SourceTier.ocr_ast,
        chunking_config={
            "parent_strategy": "pdf_page_grouped",
            "child_strategy": "sentence_merge",
            "requested_child_strategy": "sentence_merge",
            "token_budgets": {"parent_target": 1200, "child_target": 350},
            "page_ranges_preserved": True,
        },
        parents=[SimpleNamespace(parent_id="p1")],
        children=children,
        config=cfg,
        ws=WriteState(),
    )

    assert trace["file_profile"] == "digital_pdf"
    assert trace["chunking_strategy"] == "pdf_page_grouped"
    assert trace["graph_strategy"] == "compact_large_doc"
    assert trace["graph_mode"] == "compact"
    assert trace["low_value_chunk_count"] == 3
    assert trace["low_value_chunk_kinds"] == {"bibliography": 3}
    assert any("Large body chunk count" in reason for reason in trace["reasons"])


@pytest.mark.asyncio
async def test_write_mongo_persists_decision_trace():
    doc_id, corpus_id = "doc-trace", "c" * 36
    p, c = _parent(doc_id, corpus_id)
    trace = {
        "chunking_strategy": "heading_bound",
        "graph_strategy": "full_ontology",
        "low_value_chunk_count": 0,
    }
    captured: dict[str, Any] = {}

    async def _capture_doc(_db, doc):
        captured.update(doc)

    with patch.object(
        worker,
        "_find_near_duplicate_documents",
        new_callable=AsyncMock,
        return_value=[],
    ), patch.object(
        worker.mongo_writer,
        "upsert_document",
        new=AsyncMock(side_effect=_capture_doc),
    ), patch.object(
        worker.mongo_writer,
        "upsert_chunks",
        new_callable=AsyncMock,
    ):
        await worker._write_mongo_all(
            db=MagicMock(),
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id="u1",
            file_id="file1",
            filename="doc.md",
            source_tier=SourceTier.tier_a,
            source_mime="text/markdown",
            ingestion_config=IngestionConfig(),
            chunking_config={"parent_strategy": "heading_bound"},
            parents=[p],
            children=[c],
            summaries=None,
            ghost_b_out=None,
            ghost_b_failures=[],
            ghost_b_metrics=None,
            decision_trace=trace,
            ws=WriteState(),
        )

    assert captured["decision_trace"] == trace
    assert captured["decision_trace_summary"] == "heading bound - full ontology"


def test_high_signal_selector_prefers_entity_and_relation_dense_chunks():
    low = SimpleNamespace(
        chunk_id="low",
        text="This page has ordinary prose without much graph structure.",
        heading_path=["pages_10"],
        token_count=120,
    )
    high = SimpleNamespace(
        chunk_id="high",
        text=(
            "TensorFlow Lite runs on Android. ML Kit uses CameraX and "
            "classifies ImageProxy frames."
        ),
        heading_path=["Android deployment"],
        token_count=420,
    )

    selected = _select_high_signal_children([low, high], limit=1)

    assert [item.chunk_id for item in selected] == ["high"]


def test_rehydrate_ghost_b_staging_ignores_stale_extra_keys():
    staged = [
        {
            "schema_version": "polymath.extract.v1",
            "chunk_id": "c1",
            "doc_id": "d1",
            "corpus_id": "corp1",
            "extra_result_key": "legacy",
            "entities": [
                {
                    "canonical_name": "app",
                    "surface_form": "app",
                    "entity_type": "Product",
                    "confidence": 0.9,
                    "legacy_entity_key": "ignored",
                }
            ],
            "relations": [
                {
                    "subject": "app",
                    "predicate": "uses",
                    "object": "api",
                    "object_kind": "API",
                    "confidence": 0.8,
                    "legacy_relation_key": "ignored",
                }
            ],
        }
    ]

    result = worker._rehydrate_ghost_b_staging(staged)

    assert len(result) == 1
    assert result[0].entities[0].canonical_name == "app"
    assert result[0].relations[0].predicate == "uses"
    assert result[0].relations[0].object_kind == "entity"


@pytest.mark.asyncio
async def test_auto_backfill_called_when_ghost_b_failures_exist():
    ws = WriteState(mongo_written=True, qdrant_written=True, neo4j_written=True)
    failed_doc = {
        "ghost_b_failures": [{"chunk_id": "c1", "error_type": "parse_error"}],
        "write_state": ws.model_dump(),
    }
    recovered_doc = {
        "ghost_b_failures": [],
        "write_state": ws.model_dump(),
    }

    from services.ingestion import graph_backfill

    with patch.object(
        worker.mongo_reader,
        "get_document",
        new_callable=AsyncMock,
        side_effect=[failed_doc, failed_doc, recovered_doc],
    ), patch.object(
        graph_backfill,
        "backfill_failed_graph_chunks",
        new_callable=AsyncMock,
        return_value={
            "retried_chunks": 1,
            "recovered_chunks": 1,
            "remaining_failed_chunks": 0,
        },
    ) as backfill_mock:
        result = await worker._auto_backfill_graph_failures_once(
            db=MagicMock(),
            qdrant_client=MagicMock(),
            neo4j_driver=object(),
            doc_id="doc1",
            corpus_id="corp1",
            user_id="u1",
            ws=ws,
        )

    assert backfill_mock.await_count == 1
    assert result.neo4j_written is True


@pytest.mark.asyncio
async def test_graph_status_update_does_not_blank_existing_trace_strategy():
    docs = MagicMock()
    docs.update_one = AsyncMock()
    db = MagicMock()
    db.__getitem__.return_value = docs
    ws = WriteState(
        mongo_written=True,
        qdrant_written=True,
        vector_ready=True,
        graph_extraction_strategy=None,
        graph_completeness=None,
    )

    with patch.object(worker.mongo_writer, "update_write_state", new_callable=AsyncMock):
        await worker._update_graph_write_state(
            db=db,
            doc_id="doc1",
            corpus_id="corp1",
            ws=ws,
            status=worker.GRAPH_EXTRACTING,
        )

    update_doc = docs.update_one.await_args.args[1]["$set"]
    assert update_doc["decision_trace.graph_status"] == worker.GRAPH_EXTRACTING
    assert update_doc["decision_trace.vector_ready"] is True
    assert "decision_trace.graph_strategy" not in update_doc
    assert "decision_trace.graph_completeness" not in update_doc


@pytest.mark.asyncio
async def test_graph_backfill_refreshes_decision_trace_graph_fields():
    from services.ingestion import graph_backfill

    class _Cursor:
        def __init__(self, rows):
            self._rows = list(rows)

        async def to_list(self, length=None):
            return list(self._rows)

        def __aiter__(self):
            self._iter = iter(self._rows)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    doc_id = "doc1"
    corpus_id = "corp1"
    staged_result = _fake_extraction_result("c-ok", doc_id, corpus_id)
    recovered_result = _fake_extraction_result("c-retry", doc_id, corpus_id)
    doc = {
        "doc_id": doc_id,
        "corpus_id": corpus_id,
        "file_id": "file1",
        "chunk_count": 2,
        "ingestion_config": IngestionConfig(
            use_neo4j=True,
            graph_extraction_engine="llm",
        ).model_dump(),
        "ghost_b_staging": [asdict(staged_result)],
        "ghost_b_failures": [
            {
                "chunk_id": "c-retry",
                "doc_id": doc_id,
                "corpus_id": corpus_id,
                "model": "m",
                "lane": 0,
                "attempts": 1,
                "error_type": "bad_request",
                "error_message": "provider rejected old payload",
                "retryable": False,
            }
        ],
        "ghost_b_metrics": {
            "requested_chunks": 2,
            "body_children": 2,
            "extraction_strategy": "compact_large_doc",
            "graph_completeness": "needs-backfill",
        },
        "write_state": {
            "warnings": ["Ghost B graph extraction partial: old warning"],
            "graph_status": worker.GRAPH_NEEDS_BACKFILL,
        },
        "decision_trace": {
            "graph_status": worker.GRAPH_NEEDS_BACKFILL,
            "graph_strategy": "compact_large_doc",
            "graph_completeness": "needs-backfill",
        },
    }

    documents = MagicMock()
    documents.find_one = AsyncMock(return_value=doc)
    documents.update_one = AsyncMock()
    corpora = MagicMock()
    corpora.find_one = AsyncMock(return_value={"default_ingestion_config": {}})
    chunks = MagicMock()

    def _find_chunks(query, projection):
        if isinstance(query.get("chunk_id"), dict):
            return _Cursor([{"chunk_id": "c-retry", "text": "Retry text"}])
        return _Cursor([{"chunk_id": "c-ok"}, {"chunk_id": "c-retry"}])

    chunks.find.side_effect = _find_chunks
    db = MagicMock()
    db.__getitem__.side_effect = {
        "documents": documents,
        "chunks": chunks,
        "corpora": corpora,
    }.__getitem__
    report = ExtractionBatchReport(
        results=[recovered_result],
        failures=[],
        metrics={
            "requested_chunks": 1,
            "extracted_chunks": 1,
            "extraction_strategy": "compact_large_doc_backfill",
            "extraction_mode": "compact",
            "graph_completeness": "graph-complete",
            "prompt_tokens": 100,
            "attempt_count": 1,
        },
    )

    with patch.object(graph_backfill, "extract_entities", new_callable=AsyncMock, return_value=report), \
         patch.object(graph_backfill, "write_document_graph", new_callable=AsyncMock):
        result = await graph_backfill.backfill_failed_graph_chunks(
            db=db,
            qdrant_client=MagicMock(),
            neo4j_driver=object(),
            corpus_id=corpus_id,
            doc_id=doc_id,
            user_id="u1",
        )

    assert result["remaining_failed_chunks"] == 0
    update_doc = documents.update_one.await_args.args[1]["$set"]
    assert update_doc["write_state.graph_status"] == worker.GRAPH_READY
    assert update_doc["decision_trace.graph_status"] == worker.GRAPH_READY
    assert update_doc["decision_trace.graph_failed_chunks"] == 0
    assert update_doc["decision_trace.graph_requested_chunks"] == 2
    assert update_doc["decision_trace.graph_strategy"] == "compact_large_doc_backfill"
    assert update_doc["decision_trace.graph_mode"] == "compact"
    assert update_doc["decision_trace.graph_completeness"] == "graph-complete"


# ── Universal schema sanity on a Balanced config ────────────────────────────


def test_universal_schema_default_on_balanced_config():
    """IngestionConfig built with Balanced-mode flags still carries the baked
    universal entity schema — the schema is not gated by mode toggles."""
    cfg = IngestionConfig(
        use_neo4j=True, chunk_summarization=False,
        target_qdrant_collections=["naive", "hrag", "graph"],
    )
    assert cfg.entity_schema == UNIVERSAL_ENTITY_SCHEMA
    assert cfg.relation_schema == UNIVERSAL_RELATION_SCHEMA
    assert cfg.schema_strict == "soft"


# ── Integration smoke (opt-in) ──────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_deep_ingest_small_doc():
    """Live Deep-mode smoke: real Mongo + Qdrant + Neo4j + LLM. Opt-in via
    `pytest -m integration`. Requires the full docker-compose stack AND an
    ollama model that actually serves chat completions (e.g. qwen3:1.7b).
    """
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service
    from services.storage.qdrant_writer import drop_collections_for_corpus

    await conversation_service.connect()
    await ingestion_service.connect(conversation_service._db)
    db = conversation_service._db

    cfg = IngestionConfig(
        use_neo4j=True, chunk_summarization=True,
        target_qdrant_collections=["naive", "hrag", "graph"],
    )
    corpus = await ingestion_service.create_corpus(
        name="__integration_deep__", description="smoke",
        user_id="system", ingestion_config=cfg,
    )
    cid = corpus["corpus_id"]
    body = (
        b"# Apple Inc.\n\nApple Inc. was founded by Steve Jobs in 1976.\n"
        b"The company operates under HIPAA regulations.\n"
    )
    try:
        result = await ingestion_service.ingest(
            data=body, filename="apple.txt", corpus_id=cid,
            user_id="system", ingestion_config=cfg,
            model="ollama/qwen3:1.7b",
        )
        assert result.status == "done"
        ws = result.write_state.model_dump()
        assert ws["mongo_written"] is True
        assert ws["qdrant_written"] is True
        assert ws["neo4j_written"] is True
        assert ws["vector_ready"] is True
        assert ws["graph_status"] == worker.GRAPH_READY

        doc = await db["documents"].find_one(
            {"doc_id": result.doc_id, "corpus_id": cid}
        )
        assert doc is not None
        assert doc["parent_chunks"], "parent_chunks should not be empty"
        assert any(p.get("summary") for p in doc["parent_chunks"]), \
            "at least one parent should have a summary inline"
        assert isinstance(doc.get("ghost_b_staging"), list), \
            "ghost_b_staging must be persisted"

        async with ingestion_service._neo4j.session() as s:
            res = await s.run(
                "MATCH (d:Document {doc_id:$d})-[:HAS_CHUNK]->(:Chunk)"
                "-[:MENTIONS]->(e:Entity) RETURN count(DISTINCT e) AS n",
                d=result.doc_id,
            )
            rec = await res.single()
            assert rec and rec["n"] > 0, "Neo4j must hold at least one Entity"
    finally:
        await db["documents"].delete_many({"corpus_id": cid})
        await db["chunks"].delete_many({"corpus_id": cid})
        await db["corpora"].delete_one({"corpus_id": cid})
        try:
            await drop_collections_for_corpus(ingestion_service._qdrant, cid)
        except Exception:
            pass
        if ingestion_service._neo4j:
            async with ingestion_service._neo4j.session() as s:
                await s.run(
                    "MATCH (d:Document {corpus_id:$c}) DETACH DELETE d",
                    c=cid,
                )
        await ingestion_service.disconnect()
        await conversation_service.disconnect()
