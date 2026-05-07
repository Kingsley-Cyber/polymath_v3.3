"""
Worker phase-order, resume-gate, mode-toggle, and concurrency tests.

Companion to test_ghost_b_staging.py — that file owns the Ghost B staging
round-trip and resume skip path. Here we exercise:
  - phase ordering across Deep / Balanced / Fast modes
  - Ghost A resume-from-Mongo skip path
  - hard-abort behavior on Ghost A total failures / Ghost B catastrophic failures
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
    ExtractionFailureItem,
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
        recorder.events.append("ghosts_parallel")
        return summaries, ghost_b_out

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
    ensure_progress_mock = AsyncMock()
    update_state_mock = AsyncMock()
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
        patch.object(worker, "_write_neo4j_for_doc", neo4j_mock),
        patch.object(worker.mongo_reader, "get_document", get_doc_mock),
        patch.object(worker.mongo_reader, "get_corpus", get_corpus_mock),
        patch.object(worker, "_ensure_progress_document", ensure_progress_mock),
        patch.object(worker.mongo_writer, "update_write_state", update_state_mock),
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
        "neo4j": neo4j_mock,
        "get_doc": get_doc_mock,
        "ensure_progress": ensure_progress_mock,
        "update_state": update_state_mock,
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
            "parse", "chunk", "ghosts_parallel",
            "mongo_write", "embed", "qdrant_write", "neo4j_write",
        ]
    finally:
        m["stop_all"]()


@pytest.mark.asyncio
async def test_phase_order_fast_mode():
    """Fast mode: both ghosts disabled, Neo4j branch skipped entirely."""
    rec = PhaseRecorder()
    p, c = _parent("stub-doc", "c" * 36)
    # _run_ghosts_parallel still runs (it's the branching function), but both
    # branches return None. The Neo4j phase should NOT call write_document_graph.
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
        # Mongo + Qdrant still run; ghosts_parallel still called (returns None/None)
        assert rec.events == [
            "parse", "chunk", "ghosts_parallel",
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
            "parse", "chunk", "ghosts_parallel",
            "mongo_write", "embed", "qdrant_write", "neo4j_write",
        ]
        # Ghost A off, Ghost B on — summaries arg to the mongo write was None.
        kwargs = m["mongo_write"].await_args.kwargs
        assert kwargs["summaries"] is None
        assert kwargs["ghost_b_out"] == ghost_b_out
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


@pytest.mark.asyncio
async def test_ghost_a_partial_summarization_warns_and_continues():
    """A nonzero Ghost A partial should keep usable summaries and warn.

    Mongo/Qdrant/Neo4j still need a chance to run so deep ingest can retain
    search and graph coverage even if some parent summaries are absent.
    """
    doc_id, corpus_id = "doc-ghost-a-partial", "c" * 36
    p1, c1 = _parent(doc_id, corpus_id, pid="p0", child_id="c0")
    p2, c2 = _parent(doc_id, corpus_id, pid="p1", child_id="c1")
    parents = [p1, p2]
    children = [c1, c2]
    ws = WriteState()
    cfg = IngestionConfig(
        use_neo4j=False,
        chunk_summarization=True,
        target_qdrant_collections=["naive", "hrag"],
    )

    async def _partial_summarize(tasks, **kwargs):
        return [_fake_summary_result(tasks[0].parent_id, doc_id, corpus_id)]

    summarize_mock = AsyncMock(side_effect=_partial_summarize)
    with patch.object(worker, "summarize_parents", summarize_mock), \
         patch.object(worker.settings, "NEO4J_ENABLED", True):
        result = await worker._run_ghosts_parallel(
            config=cfg, parents=parents, children=children,
            doc_id=doc_id, corpus_id=corpus_id,
            model="m",
            db=MagicMock(), qdrant_client=MagicMock(),
            neo4j_driver=MagicMock(),
            existing_doc=None, ws=ws,
        )

    assert summarize_mock.await_count == 1
    assert result.summaries is not None
    assert len(result.summaries) == 1
    assert result.ghost_b_out is None
    assert result.warnings
    assert result.warnings[0].startswith("Ghost A parent summarization partial: 1/2")


@pytest.mark.asyncio
async def test_ghost_a_zero_summaries_still_hard_aborts():
    """Zero Ghost A output is treated as provider outage and aborts the doc."""
    doc_id, corpus_id = "doc-ghost-a-zero", "c" * 36
    p1, c1 = _parent(doc_id, corpus_id, pid="p0", child_id="c0")
    p2, c2 = _parent(doc_id, corpus_id, pid="p1", child_id="c1")
    parents = [p1, p2]
    children = [c1, c2]
    ws = WriteState()
    cfg = IngestionConfig(
        use_neo4j=False,
        chunk_summarization=True,
        target_qdrant_collections=["naive", "hrag"],
    )

    summarize_mock = AsyncMock(return_value=[])
    with patch.object(worker, "summarize_parents", summarize_mock), \
         patch.object(worker.settings, "NEO4J_ENABLED", True), \
         pytest.raises(GhostAFailure, match="Ghost A produced 0/2 summaries"):
        await worker._run_ghosts_parallel(
            config=cfg, parents=parents, children=children,
            doc_id=doc_id, corpus_id=corpus_id,
            model="m",
            db=MagicMock(), qdrant_client=MagicMock(),
            neo4j_driver=MagicMock(),
            existing_doc=None, ws=ws,
        )


@pytest.mark.asyncio
async def test_ghost_b_zero_extractions_leaves_neo4j_pending():
    """A total Ghost B outage should not produce a chunk-only Neo4j success.

    Mongo/Qdrant can still proceed after the ghost phase, but returning
    ghost_b_out=None prevents the Neo4j writer from flipping neo4j_written
    when no entity/relation extraction was produced.
    """
    doc_id, corpus_id = "doc-ghost-b-zero", "c" * 36
    p1, c1 = _parent(doc_id, corpus_id, pid="p0", child_id="c0")
    p2, c2 = _parent(doc_id, corpus_id, pid="p1", child_id="c1")
    parents = [p1, p2]
    children = [c1, c2]
    ws = WriteState()
    cfg = IngestionConfig(
        use_neo4j=True,
        chunk_summarization=False,
        target_qdrant_collections=["naive", "hrag", "graph"],
    )
    failure = ExtractionFailureItem(
        chunk_id="c0",
        doc_id=doc_id,
        corpus_id=corpus_id,
        model="m",
        lane=0,
        attempts=2,
        error_type="parse_error",
        error_message="parse returned None",
    )
    report = ExtractionBatchReport(
        results=[],
        failures=[failure],
        metrics={
            "requested_chunks": 2,
            "extracted_chunks": 0,
            "failed_chunks": 1,
            "success_rate": 0.0,
            "error_counts": {"parse_error": 1},
        },
    )

    extract_mock = AsyncMock(return_value=report)
    lens = SimpleNamespace(to_dict=lambda: {"lens_id": "test-lens"})
    with patch.object(worker, "extract_entities", extract_mock), \
         patch.object(worker, "get_or_create_schema_lens", AsyncMock(return_value=lens)), \
         patch.object(worker.settings, "NEO4J_ENABLED", True):
        result = await worker._run_ghosts_parallel(
            config=cfg, parents=parents, children=children,
            doc_id=doc_id, corpus_id=corpus_id,
            model="m",
            db=MagicMock(), qdrant_client=MagicMock(),
            neo4j_driver=MagicMock(),
            existing_doc=None, ws=ws,
        )

    assert extract_mock.await_count == 1
    assert result.ghost_b_out is None
    assert result.ghost_b_failures == [failure]
    assert result.ghost_b_metrics is not None
    assert result.ghost_b_metrics["extracted_chunks"] == 0
    assert result.warnings
    assert result.warnings[0].startswith("Ghost B graph extraction produced 0/2")


# ── Hard-abort tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ghost_a_hard_abort_raises_and_skips_writes():
    """A catastrophic/zero Ghost A failure raises GhostAFailure.

    run_ingest_job must not write to Mongo / Qdrant / Neo4j, and
    write_state flags must remain at their pre-job values."""
    rec = PhaseRecorder()
    p, c = _parent("stub-doc", "c" * 36)
    m = _install_mocks(rec, parents=[p], children=[c],
                       summaries=None, ghost_b_out=None)
    # Replace the mocked _run_ghosts_parallel with one that raises.
    async def _raise(**kw):
        rec.events.append("ghosts_parallel")
        raise GhostAFailure("Ghost A produced 0/1 summaries; treating as provider outage")
    worker._run_ghosts_parallel = _raise  # type: ignore[assignment]
    try:
        cfg = IngestionConfig(use_neo4j=False, chunk_summarization=True)
        with pytest.raises(GhostAFailure):
            await _run_job(m, cfg)
        # No post-ghost phase fired
        for phase in ("mongo_write", "embed", "qdrant_write", "neo4j_write"):
            assert phase not in rec.events
        # No write_state flag flip
        assert m["update_state"].await_count == 0
    finally:
        m["stop_all"]()


@pytest.mark.asyncio
async def test_ghost_b_hard_abort_raises_and_skips_writes():
    """Ghost B partial → GhostBFailure on fresh ingest. Under the refactored
    locked order, ghosts run BEFORE Mongo write, so mongo_written stays False.
    (The prompt spec's 'mongo_written stays true' note predates the reorder.)"""
    rec = PhaseRecorder()
    p, c = _parent("stub-doc", "c" * 36)
    m = _install_mocks(rec, parents=[p], children=[c],
                       summaries=None, ghost_b_out=None)
    async def _raise(**kw):
        rec.events.append("ghosts_parallel")
        raise GhostBFailure("Ghost B partial: 0/1")
    worker._run_ghosts_parallel = _raise  # type: ignore[assignment]
    try:
        cfg = IngestionConfig(use_neo4j=True, chunk_summarization=False,
                              target_qdrant_collections=["naive", "hrag", "graph"])
        with pytest.raises(GhostBFailure):
            await _run_job(m, cfg)
        for phase in ("mongo_write", "embed", "qdrant_write", "neo4j_write"):
            assert phase not in rec.events
        assert m["update_state"].await_count == 0
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
        assert ws == {"mongo_written": True, "qdrant_written": True,
                      "neo4j_written": True}

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
