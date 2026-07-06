from __future__ import annotations

import statistics
import time

import pytest

from models.schemas import SourceChunk
from services.context_manager import context_manager
from services.ingestion.doc_artifact import (
    ARTIFACT_VERSION,
    build_doc_artifact,
    format_source_role_header,
)
from services.retriever.waterfall import DocNote, ParentCandidate, allocate


def _profile(summary: str = "Seedance prompt settings and camera workflow.") -> dict:
    return {
        "summary": summary,
        "concepts": ["Seedance", "camera motion", "prompting"],
        "domains": {"video_generation": 3},
        "schema_version": "polymath.summary_tree.v1",
    }


def test_doc_artifact_preserves_owner_intent_and_detects_model_scope():
    artifact = build_doc_artifact(
        doc_profile=_profile(),
        source_meta={"filename": "seedance-prompt-guide.md", "title": "Seedance Prompt Guide"},
        ghost_b_entities=[{"canonical_name": "Seedance"}, {"surface_form": "camera movement"}],
        chunk_kind_stats={"body": 12},
        owner_fields={"owner_intent": "Use this as my house style for ads."},
        corpus_description="Fallback corpus description should not overwrite owner note.",
    )

    assert artifact is not None
    assert artifact["artifact_version"] == ARTIFACT_VERSION
    assert artifact["owner_intent"] == "Use this as my house style for ads."
    assert artifact["field_provenance"]["owner_intent"] == "owner"
    assert artifact["model_scope"] == ["Seedance"]
    assert artifact["field_provenance"]["model_scope"] == "deterministic"
    assert "model_specific_advice" in artifact["source_role"]
    assert artifact["field_provenance"]["synthesis_hint"] == "template"


def test_doc_artifact_uses_corpus_description_only_when_owner_missing():
    artifact = build_doc_artifact(
        doc_profile=_profile("Cinematic directing theory and lighting techniques."),
        source_meta={"filename": "directing-book.md"},
        owner_fields={},
        corpus_description="User says this corpus is for cinematic language.",
    )

    assert artifact is not None
    assert artifact["owner_intent"] == "User says this corpus is for cinematic language."
    assert artifact["field_provenance"]["owner_intent"] == "corpus_description"


def test_doc_artifact_recompile_does_not_promote_corpus_fallback_to_owner():
    first = build_doc_artifact(
        doc_profile=_profile("Cinematic directing theory and lighting techniques."),
        corpus_description="Initial corpus note.",
    )

    second = build_doc_artifact(
        doc_profile=_profile("Cinematic directing theory and lighting techniques."),
        owner_fields=first,
        corpus_description="Updated corpus note.",
    )

    assert second is not None
    assert second["owner_intent"] == "Updated corpus note."
    assert second["field_provenance"]["owner_intent"] == "corpus_description"


def test_doc_artifact_preserves_owner_sourced_role_and_scope_on_recompile():
    artifact = build_doc_artifact(
        doc_profile=_profile("Seedance prompt settings and camera workflow."),
        source_meta={"filename": "seedance-reference.md"},
        ghost_b_entities=[{"canonical_name": "Seedance"}],
        owner_fields={
            "source_role": ["technique_theory"],
            "model_scope": ["House Model"],
            "field_provenance": {
                "source_role": "owner",
                "model_scope": "owner",
            },
        },
    )

    assert artifact is not None
    assert artifact["source_role"] == ["technique_theory"]
    assert artifact["model_scope"] == ["House Model"]
    assert artifact["field_provenance"]["source_role"] == "owner"
    assert artifact["field_provenance"]["model_scope"] == "owner"
    assert artifact["confidence"]["source_role"] == 1.0
    assert artifact["confidence"]["model_scope"] == 1.0


def test_doc_artifact_ambiguous_general_docs_are_low_confidence():
    artifact = build_doc_artifact(
        doc_profile={
            "summary": "A broad essay about creativity and planning.",
            "concepts": ["creativity", "planning"],
            "domains": {"general": 1},
            "schema_version": "polymath.summary_tree.v1",
        },
        source_meta={"filename": "notes.md"},
        ghost_b_entities=[],
        chunk_kind_stats={"body": 4},
    )

    assert artifact is not None
    assert artifact["source_role"] == ["general_context"]
    assert artifact["model_scope"] is None
    assert artifact["confidence"]["source_role"] < 0.5
    assert artifact["field_provenance"]["source_role"] == "none"


def test_doc_artifact_missing_profile_is_noop():
    assert build_doc_artifact(None) is None
    assert build_doc_artifact({}) is None


def test_source_role_header_is_context_not_evidence():
    artifact = build_doc_artifact(
        doc_profile=_profile(),
        source_meta={"filename": "kling-guide.md", "title": "Kling camera workflow"},
        ghost_b_entities=["Kling"],
        owner_fields={"owner_intent": "Prefer this for product shots."},
    )

    header = format_source_role_header("kling-guide.md", artifact)

    assert header.startswith('[Source: "kling-guide.md"')
    assert "role:" in header
    assert "model scope: Kling" in header
    assert 'owner note: "Prefer this for product shots."' in header
    assert "context only, not citable evidence" in header


def test_context_manager_renders_doc_artifact_header_once_per_doc():
    artifact = build_doc_artifact(
        doc_profile=_profile(),
        source_meta={"filename": "seedance-guide.md"},
        ghost_b_entities=["Seedance"],
    )
    sources = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="doc-1",
            corpus_id="corpus-1",
            text="Seedance supports detailed camera motion prompts.",
            score=0.9,
            source_tier="child",
            doc_name="seedance-guide.md",
            metadata={"doc_artifact": artifact},
        ),
        SourceChunk(
            chunk_id="c2",
            parent_id="p2",
            doc_id="doc-1",
            corpus_id="corpus-1",
            text="Use precise subject and scene phrasing.",
            score=0.8,
            source_tier="child",
            doc_name="seedance-guide.md",
            metadata={"doc_artifact": artifact},
        ),
    ]

    prompt = context_manager.build_augmented_prompt("Create a prompt for a product video.", sources)

    assert prompt.count('[Source: "seedance-guide.md"') == 1
    assert "context only, not citable evidence" in prompt
    assert "<generative_task_policy>" in prompt
    assert 'from "seedance-guide.md": Seedance supports detailed camera motion prompts.' in prompt


def test_context_manager_omits_header_when_artifact_absent():
    source = SourceChunk(
        chunk_id="c1",
        parent_id="p1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text="Plain evidence.",
        score=0.9,
        source_tier="child",
        doc_name="plain.md",
    )

    prompt = context_manager.build_augmented_prompt("What does this say?", [source])

    assert '[Source: "plain.md"' not in prompt
    assert 'from "plain.md": Plain evidence.' in prompt


def test_doc_note_drops_before_evidence_under_budget_pressure():
    parent = ParentCandidate(
        parent_id="p1",
        doc_id="doc-1",
        score=0.9,
        full_text="evidence " * 20,
        summary="short evidence",
    )
    note = DocNote(doc_id="doc-1", text="[Source: note context only, not citable evidence]")

    tight = allocate([parent], budget_tokens=3, doc_notes=[note], count_tokens=lambda s: len(s.split()))
    roomy = allocate([parent], budget_tokens=12, doc_notes=[note], count_tokens=lambda s: len(s.split()))

    assert [item.kind for item in tight.items] == ["summary"]
    assert "doc_note" not in [item.kind for item in tight.items]
    assert "doc_note" in [item.kind for item in roomy.items]


def test_doc_note_does_not_block_summary_promotion():
    parent = ParentCandidate(
        parent_id="p1",
        doc_id="doc-1",
        score=0.9,
        full_text="full evidence text wins",
        summary="summary",
    )
    note = DocNote(doc_id="doc-1", text="passive note consumes spare tokens")

    packet = allocate([parent], budget_tokens=4, doc_notes=[note], count_tokens=lambda s: len(s.split()))

    assert [item.kind for item in packet.items] == ["full"]
    assert packet.items[0].text == "full evidence text wins"


def test_artifact_header_latency_p50_under_20ms():
    artifact = build_doc_artifact(
        doc_profile=_profile(),
        source_meta={"filename": "seedance-guide.md"},
        ghost_b_entities=["Seedance"],
    )
    sources = [
        SourceChunk(
            chunk_id=f"c{i}",
            parent_id=f"p{i}",
            doc_id=f"doc-{i % 5}",
            corpus_id="corpus-1",
            text="Seedance prompt evidence.",
            score=0.9,
            source_tier="child",
            doc_name=f"doc-{i % 5}.md",
            metadata={"doc_artifact": artifact},
        )
        for i in range(20)
    ]

    timings = []
    for _ in range(25):
        start = time.perf_counter()
        context_manager.build_augmented_prompt("Create a video prompt.", sources)
        timings.append(time.perf_counter() - start)

    assert statistics.median(timings) < 0.020


def test_passive_artifact_does_not_change_retrieved_chunk_identity():
    plain = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="doc-1",
            corpus_id="corpus-1",
            text="Evidence.",
            score=0.9,
            source_tier="child",
        )
    ]
    with_artifact = [plain[0].model_copy(update={"metadata": {"doc_artifact": build_doc_artifact(_profile())}})]

    context_manager.build_augmented_prompt("Explain the source.", plain)
    context_manager.build_augmented_prompt("Explain the source.", with_artifact)

    assert [chunk.chunk_id for chunk in plain] == [chunk.chunk_id for chunk in with_artifact]


@pytest.mark.asyncio
async def test_retriever_selection_is_identical_with_doc_artifacts(monkeypatch):
    """Artifact hydration may decorate returned chunks, but must not alter selection."""

    import services.retriever as retriever_mod
    from models.schemas import RetrievalTier

    artifact = build_doc_artifact(_profile())
    base_candidates = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="doc-1",
            corpus_id="corpus-1",
            text="Seedance camera prompt evidence.",
            score=0.91,
            source_tier="qdrant_child",
        ),
        SourceChunk(
            chunk_id="c2",
            parent_id="p2",
            doc_id="doc-2",
            corpus_id="corpus-1",
            text="Workflow evidence.",
            score=0.83,
            source_tier="qdrant_child",
        ),
    ]

    async def fake_embed(_query, _config=None):
        return [0.1, 0.2, 0.3]

    async def empty_search(*_args, **_kwargs):
        return []

    async def child_search(*_args, **_kwargs):
        return [chunk.model_copy(deep=True) for chunk in base_candidates]

    async def identity_hydrate_rerank(chunks, *_args, **_kwargs):
        return [chunk.model_copy(deep=True) for chunk in chunks]

    async def keep_corpora(ids):
        return ids, []

    async def keep_tier(tier, _ids):
        return tier, None

    async def no_embedding_config(_ids):
        return None

    hydrate_with_artifacts = False

    async def fake_hydrate(chunks, *_args, **_kwargs):
        hydrated = [chunk.model_copy(deep=True) for chunk in chunks]
        if hydrate_with_artifacts:
            for chunk in hydrated:
                metadata = dict(chunk.metadata or {})
                metadata["doc_artifact"] = artifact
                chunk.metadata = metadata
        return hydrated

    orchestrator = retriever_mod.RetrieverOrchestrator()
    monkeypatch.setattr(orchestrator, "_filter_existing_corpora", keep_corpora)
    monkeypatch.setattr(orchestrator, "_enforce_strategy_intersection", keep_tier)
    monkeypatch.setattr(orchestrator, "_embedding_config_for_query", no_embedding_config)
    monkeypatch.setattr(retriever_mod, "embed_query", fake_embed)
    monkeypatch.setattr(retriever_mod.funnel_a, "search", empty_search)
    monkeypatch.setattr(retriever_mod.funnel_b, "search", child_search)
    monkeypatch.setattr(retriever_mod.lexical_retriever, "search", empty_search)
    monkeypatch.setattr(retriever_mod.document_anchor_retriever, "search", empty_search)
    monkeypatch.setattr(retriever_mod, "hydrate_rerank_texts", identity_hydrate_rerank)
    monkeypatch.setattr(retriever_mod, "hydrate_chunks", fake_hydrate)

    kwargs = {
        "query": "create a Seedance product video prompt",
        "corpus_ids": ["corpus-1"],
        "retrieval_tier": RetrievalTier.qdrant_mongo,
        "collections": None,
        "retrieval_k": 2,
        "rerank_enabled": False,
        "top_k_summary": 0,
        "final_top_k": 2,
        "support_profile": True,
    }

    without = await orchestrator._retrieve_uncached(**kwargs)
    hydrate_with_artifacts = True
    with_artifact = await orchestrator._retrieve_uncached(**kwargs)

    assert [chunk.chunk_id for chunk in without.chunks] == ["c1", "c2"]
    assert [chunk.chunk_id for chunk in with_artifact.chunks] == ["c1", "c2"]
    assert [chunk.chunk_id for chunk in without.chunks] == [
        chunk.chunk_id for chunk in with_artifact.chunks
    ]
    assert all("doc_artifact" in (chunk.metadata or {}) for chunk in with_artifact.chunks)
